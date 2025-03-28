from __future__ import annotations

import argparse
from typing import Literal

import numpy as np
import torch
import torch.nn.functional as F
from relbench.base import RecommendationTask
from relbench.tasks import get_task
from torch import Tensor
from torch_frame.data.multi_tensor import _batched_arange
from torch_geometric.seed import seed_everything
from torch_geometric.utils import coalesce
from tqdm import tqdm


# The metric for link prediction
LINK_PREDICTION_METRIC = 'link_prediction_map'
# The absolute tolerance for validation map for early stopping
VAL_MAP_ATOL = 0.001
# The minimum number of epochs to run before early stopping
MIN_EPOCHS = 10

# The total number of optimization step count for annealing
total_optimization_steps = 0


class MultiVAE(torch.nn.Module):
    def __init__(
        self,
        p_dims: list[int],
        q_dims: list[int] | None = None,
        dropout: float = 0.5,
    ) -> None:
        super().__init__()
        if q_dims and q_dims[0] != p_dims[-1]:
            raise ValueError('In and Out dimensions must equal to each other.')
        if q_dims and q_dims[-1] != p_dims[0]:
            raise ValueError(
                'Latent dimension for p- and q- network mismatches.')

        self.p_dims = p_dims
        self.q_dims = q_dims or p_dims[::-1]
        self.dropout = dropout
        self.p_layers = torch.nn.ModuleList([
            torch.nn.Linear(d_in, d_out)
            for d_in, d_out in zip(self.p_dims[:-1], self.p_dims[1:])
        ])
        self.q_layers = torch.nn.ModuleList([
            torch.nn.Linear(d_in, d_out) for d_in, d_out in zip(
                self.q_dims[:-1],
                # NOTE: Double the last dim of the encoder for mean and logvar,
                # i.e., [q1, ..., qn] -> [q1, ..., qn*2].
                self.q_dims[1:-1] + [self.q_dims[-1] * 2],
            )
        ])
        self.reset_parameters()

    def reset_parameters(self) -> None:
        for layer in self.modules():
            if isinstance(layer, torch.nn.Linear):
                torch.nn.init.xavier_uniform_(layer.weight)
                torch.nn.init.trunc_normal_(layer.bias, std=0.001)

    def forward(self, x: Tensor) -> tuple[Tensor, Tensor, Tensor]:
        mu, logvar = self.encode(x)
        z = self.reparameterize(mu, logvar)
        return self.decode(z), mu, logvar

    def encode(self, x: Tensor) -> tuple[Tensor, Tensor]:
        """Returns mean and logvar of q(z|x)."""
        h = F.normalize(x)
        h = F.dropout(h, p=self.dropout, training=self.training)
        for i, layer in enumerate(self.q_layers):
            h = layer(h)
            if i != len(self.q_layers) - 1:
                h = F.tanh(h)
        mu, logvar = h[:, :self.q_dims[-1]], h[:, self.q_dims[-1]:]
        return mu, logvar

    def reparameterize(self, mu: Tensor, logvar: Tensor) -> Tensor:
        """Returns a sample from q(z|x) and the mean during training and
        inference, respectively.
        """
        if self.training:
            std = torch.exp(0.5 * logvar)
            eps = torch.randn_like(std)  # sample more if necessary
            return mu + eps * std
        else:
            return mu  # Use mean for inference

    def decode(self, z: Tensor) -> Tensor:
        """Decodes z to a probability distribution over items."""
        h = z
        for i, layer in enumerate(self.p_layers):
            h = layer(h)
            if i != len(self.p_layers) - 1:
                h = F.tanh(h)
        return h


def loss_function(
    recon_x: Tensor,
    x: Tensor,
    mu: Tensor,
    logvar: Tensor,
    anneal: float = 1.0,
) -> Tensor:
    recon_loss = -torch.mean(torch.sum(F.log_softmax(recon_x, 1) * x, dim=-1))
    kl = -0.5 * torch.mean(
        torch.sum(1 + logvar - mu.pow(2) - logvar.exp(), dim=1))
    return recon_loss + anneal * kl


def train(
    model: MultiVAE,
    optimizer: torch.optim.Optimizer,
    data_dict: dict[str, tuple[Tensor, Tensor]],
    device: torch.device,
    args,
    task: RecommendationTask,
) -> float:
    rowptr, edge_index = data_dict['train']
    col = edge_index[1]
    model.train()
    global total_optimization_steps
    N = len(rowptr) - 1
    idxlist = list(range(N))
    np.random.shuffle(idxlist)
    accum_loss = torch.zeros(1, device=device)
    for start in tqdm(range(0, N, args.batch_size), desc='train'):
        end = min(start + args.batch_size, N)
        batch_size = end - start
        src_index = torch.tensor(
            idxlist[start:end],
            dtype=torch.int64,
            device=device,
        )
        src_batch, dst_index = get_rhs_index(src_index, rowptr, col)
        # convert rowptr and col to a dense tensor of ones:
        x = torch.zeros(
            (batch_size, task.num_dst_nodes),
            dtype=torch.float32,
            device=device,
        )
        x[src_batch, dst_index] = 1.0
        if args.total_anneal_steps > 0:
            anneal = min(
                args.anneal_cap,
                total_optimization_steps / args.total_anneal_steps,
            )
        else:
            anneal = args.anneal_cap
        recon_x, mu, logvar = model(x)
        loss = loss_function(recon_x, x, mu, logvar, anneal)
        loss.backward()
        optimizer.step()
        optimizer.zero_grad()
        total_optimization_steps += 1
        accum_loss += loss.detach()
    return float(accum_loss / N)


def get_rhs_index(
    src_index: Tensor,
    rowptr: Tensor,
    col: Tensor,
) -> tuple[Tensor, Tensor]:
    src_batch, arange = _batched_arange(rowptr[src_index + 1] -
                                        rowptr[src_index])
    dst_index = col[arange + rowptr[src_index][src_batch]]
    return src_batch, dst_index


@torch.no_grad()
def test(
    model: MultiVAE,
    data_dict: dict[str, tuple[Tensor, Tensor]],
    device: torch.device,
    args,
    task: RecommendationTask,
    stage: Literal['val', 'test'],
) -> tuple[float, float]:
    model.eval()
    # Validating uses training set as the input
    # Testing uses training and validation sets as the input
    if stage == 'val':
        rowptr, edge_index = data_dict['train']
        col = edge_index[1]
        _, test_edge_index = data_dict['val']
    elif stage == 'test':
        # Combine train and val set:
        _, edge_index_train = data_dict['train']
        _, edge_index_val = data_dict['val']
        edge_index = torch.cat([edge_index_train, edge_index_val], dim=1)
        edge_index = coalesce(edge_index)
        rowptr = torch._convert_indices_from_coo_to_csr(
            input=edge_index[0],
            size=task.num_src_nodes,
        )
        col = edge_index[1]
        _, test_edge_index = data_dict['test']
    else:
        raise ValueError(f'Invalid stage: {stage}')

    N = len(rowptr) - 1
    idxlist = list(range(N))
    pred_list: list[Tensor] = []
    for start in tqdm(range(0, N, args.batch_size), desc=stage):
        end = min(start + args.batch_size, N)
        src_index = torch.tensor(
            idxlist[start:end],
            dtype=torch.int64,
            device=device,
        )
        lhs_eval_mask = torch.isin(src_index, test_edge_index[0])
        src_index = src_index[lhs_eval_mask]
        if len(src_index) == 0:
            continue
        src_batch, dst_index = get_rhs_index(src_index, rowptr, col)
        # convert rowptr and col to a dense tensor of ones:
        batch_size = len(src_index)
        x = torch.zeros(
            (batch_size, task.num_dst_nodes),
            dtype=torch.float32,
            device=device,
        )
        x[src_batch, dst_index] = 1.0
        recon_x, _, _ = model(x)
        scores = torch.sigmoid(recon_x)
        _, pred_mini = torch.topk(scores, k=task.eval_k, dim=1)
        pred_list.append(pred_mini)

    pred = torch.cat(pred_list, dim=0).cpu().numpy()
    res = task.evaluate(pred, task.get_table(stage))
    # TODO: Return loss
    return 0.0, float(res[LINK_PREDICTION_METRIC])


def load_data_dict(
    task: RecommendationTask,
    device: torch.device,
) -> dict[str, tuple[Tensor, Tensor]]:
    data_dict: dict[str, tuple[Tensor, Tensor]] = {}
    for split in ['train', 'val', 'test']:
        split_df = task.get_table(split).df.drop(
            columns=['timestamp']).explode(task.dst_entity_col)
        edge_index = torch.tensor(
            [
                split_df[task.src_entity_col].values,
                split_df[task.dst_entity_col].values,
            ],
            dtype=torch.int64,
            device=device,
        )
        edge_index = coalesce(edge_index)
        rowptr = torch._convert_indices_from_coo_to_csr(
            input=edge_index[0],
            size=task.num_src_nodes,
        )
        data_dict[split] = (rowptr, edge_index)
    return data_dict


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--dataset',
        type=str,
        default='rel-trial',
        choices=[
            'rel-trial',
            'rel-amazon',
            'rel-stack',
            'rel-avito',
            'rel-hm',
        ],
    )
    parser.add_argument('--task', type=str, default='site-sponsor-run')
    parser.add_argument('--lr', type=float, default=1e-4)
    parser.add_argument('--wd', type=float, default=0.00)
    parser.add_argument('--batch_size', type=int, default=1024)
    parser.add_argument('--epochs', type=int, default=200)
    parser.add_argument('--total_anneal_steps', type=int, default=200_000)
    parser.add_argument('--anneal_cap', type=float, default=0.2)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--result_path', type=str, default='result.pt')
    args = parser.parse_args()
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model_filename = f'multi_vae_{args.dataset}_{args.task}.pt'
    task: RecommendationTask = get_task(args.dataset, args.task, download=True)
    data_dict = load_data_dict(task, device)
    seed_everything(args.seed)
    model = MultiVAE(
        p_dims=[200, 600, task.num_dst_nodes],
        q_dims=None,
    ).to(device)
    optimizer = torch.optim.Adam(
        model.parameters(),
        lr=args.lr,
        weight_decay=args.wd,
    )
    best_val_map = 0.0
    for epoch in range(1, args.epochs + 1):
        train_loss = train(
            model,
            optimizer,
            data_dict,
            device,
            args,
            task,
        )
        val_loss, val_map = test(
            model,
            data_dict,
            device,
            args,
            task,
            'val',
        )

        if val_map > best_val_map:
            best_val_map = val_map
            torch.save(model, model_filename)

        print(f'Epoch {epoch:3d}: '
              f'train_loss {train_loss:4.4f}, '
              f'val_map {val_map:4.4f}, '
              f'best_val_map {best_val_map:4.4f}')

        should_check_early_stop = epoch > MIN_EPOCHS
        if should_check_early_stop and val_map < best_val_map - VAL_MAP_ATOL:
            print(f'Best val: {best_val_map:.4f}')
            break

    model = torch.load(model_filename).to(device)
    test_loss, test_map = test(
        model,
        data_dict,
        device,
        args,
        task,
        'test',
    )
    print(f'best_val_map: {best_val_map}, test_map: {test_map}')


if __name__ == '__main__':
    main()
