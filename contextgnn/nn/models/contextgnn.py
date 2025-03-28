from typing import Any, Dict, Optional, Tuple, Type

import torch
from torch import Tensor
from torch_frame.data.stats import StatType
from torch_frame.nn.models import ResNet
from torch_geometric.data import HeteroData
from torch_geometric.nn import MLP
from torch_geometric.typing import NodeType
from torch_geometric.utils.map import map_index
from typing_extensions import Self

from contextgnn.nn.encoder import (
    DEFAULT_STYPE_ENCODER_DICT,
    HeteroEncoder,
    HeteroTemporalEncoder,
)
from contextgnn.nn.models import HeteroGraphSAGE
from contextgnn.nn.models.rhsembeddinggnn import RHSEmbeddingGNN
from contextgnn.utils import RHSEmbeddingMode


class ContextGNN(RHSEmbeddingGNN):
    r"""Implementation of ContextGNN model."""
    def __init__(
        self,
        data: HeteroData,
        col_stats_dict: Dict[str, Dict[str, Dict[StatType, Any]]],
        rhs_emb_mode: RHSEmbeddingMode,
        dst_entity_table: str,
        num_nodes: int,
        num_layers: int,
        channels: int,
        embedding_dim: int,
        aggr: str = 'sum',
        norm: str = 'layer_norm',
        torch_frame_model_cls: Type[torch.nn.Module] = ResNet,
        torch_frame_model_kwargs: Optional[Dict[str, Any]] = None,
        rhs_sample_size: Optional[int] = None,
    ) -> None:
        super().__init__(data, col_stats_dict, rhs_emb_mode, dst_entity_table,
                         num_nodes, embedding_dim)

        self.encoder = HeteroEncoder(
            channels=channels,
            node_to_col_names_dict={
                node_type: data[node_type].tf.col_names_dict
                for node_type in data.node_types
            },
            node_to_col_stats=col_stats_dict,
            stype_encoder_cls_kwargs=DEFAULT_STYPE_ENCODER_DICT,
            torch_frame_model_cls=torch_frame_model_cls,
            torch_frame_model_kwargs=torch_frame_model_kwargs,
        )
        self.temporal_encoder = HeteroTemporalEncoder(
            node_types=[
                node_type for node_type in data.node_types
                if "time" in data[node_type]
            ],
            channels=channels,
        )
        self.gnn = HeteroGraphSAGE(
            node_types=data.node_types,
            edge_types=data.edge_types,
            channels=channels,
            aggr=aggr,
            num_layers=num_layers,
        )
        self.head = MLP(
            channels,
            out_channels=1,
            norm=norm,
            num_layers=1,
        )
        self.lhs_projector = torch.nn.Linear(channels, embedding_dim)

        self.id_awareness_emb = torch.nn.Embedding(1, channels)
        self.lin_offset_idgnn = torch.nn.Linear(embedding_dim, 1)
        self.lin_offset_embgnn = torch.nn.Linear(embedding_dim, 1)
        self.channels = channels
        self.num_rhs_nodes = num_nodes
        self.rhs_sample_size = rhs_sample_size

        self.reset_parameters()

    def reset_parameters(self) -> None:
        super().reset_parameters()
        self.encoder.reset_parameters()
        self.temporal_encoder.reset_parameters()
        self.gnn.reset_parameters()
        self.head.reset_parameters()
        self.id_awareness_emb.reset_parameters()
        self.rhs_embedding.reset_parameters()
        self.lin_offset_embgnn.reset_parameters()
        self.lin_offset_idgnn.reset_parameters()
        self.lhs_projector.reset_parameters()

    def sample_step(self, rhs_idgnn_index, lhs_idgnn_batch, rhs_gnn_embedding,
                    lhs_y_batch, rhs_y_index):
        rnd = torch.rand(self.num_rhs_nodes, device=rhs_idgnn_index.device)
        # Prioritize idgnn logits
        rnd[rhs_idgnn_index] = 3.
        # Ensure we always sample positives
        rhs_y_index = rhs_y_index
        assert rhs_y_index is not None  # always pass in dst index
        rnd[rhs_y_index] = 4.
        rhs_index = rnd.topk(self.rhs_sample_size, sorted=True).indices
        inclusive = rhs_y_index.numel() <= self.rhs_sample_size
        rhs_y_index, mask = map_index(rhs_y_index, rhs_index,
                                      max_index=self.num_rhs_nodes,
                                      inclusive=inclusive)
        lhs_y_batch = lhs_y_batch if inclusive else lhs_y_batch[mask]
        rhs_embedding = self.rhs_embedding(rhs_index)  # num_rhs_nodes, channel
        inclusive = (rhs_y_index.numel() + rhs_idgnn_index.numel()
                     <= self.rhs_sample_size)
        rhs_idgnn_index, mask = map_index(rhs_idgnn_index, rhs_index,
                                          inclusive=inclusive)
        if not inclusive:
            lhs_idgnn_batch = lhs_idgnn_batch[mask]
            rhs_gnn_embedding = rhs_gnn_embedding[mask]
        return (rhs_idgnn_index, rhs_embedding, lhs_idgnn_batch,
                rhs_gnn_embedding, lhs_y_batch, rhs_y_index)

    def construct_logits(self, lhs_embedding_projected, lhs_embedding,
                         rhs_gnn_embedding, rhs_embedding, lhs_idgnn_batch,
                         rhs_idgnn_index):
        embgnn_logits = lhs_embedding_projected @ rhs_embedding.t(
        )  # batch_size, num_rhs_nodes

        # Model the importance of embedding-GNN prediction for each lhs node
        embgnn_offset_logits = self.lin_offset_embgnn(
            lhs_embedding_projected).flatten()
        embgnn_logits += embgnn_offset_logits.view(-1, 1)

        # Calculate idgnn logits
        idgnn_logits = self.head(
            rhs_gnn_embedding).flatten()  # num_sampled_rhs
        # Because we are only doing 2 hop, we are not really sampling info from
        # lhs therefore, we need to incorporate this information using
        # lhs_embedding[lhs_idgnn_batch] * rhs_gnn_embedding
        idgnn_logits += (
            lhs_embedding[lhs_idgnn_batch] *  # num_sampled_rhs, channel
            rhs_gnn_embedding).sum(
                dim=-1).flatten()  # num_sampled_rhs, channel

        # Model the importance of ID-GNN prediction for each lhs node
        idgnn_offset_logits = self.lin_offset_idgnn(
            lhs_embedding_projected).flatten()
        idgnn_logits = idgnn_logits + idgnn_offset_logits[lhs_idgnn_batch]

        embgnn_logits[lhs_idgnn_batch, rhs_idgnn_index] = idgnn_logits
        return embgnn_logits

    def forward_gnn(
        self,
        batch: HeteroData,
        entity_table: NodeType,
    ):
        seed_time = batch[entity_table].seed_time
        x_dict = self.encoder(batch.tf_dict)

        # Add ID-awareness to the root node
        x_dict[entity_table][:seed_time.size(0
                                             )] += self.id_awareness_emb.weight
        rel_time_dict = self.temporal_encoder(seed_time, batch.time_dict,
                                              batch.batch_dict)

        for node_type, rel_time in rel_time_dict.items():
            x_dict[node_type] = x_dict[node_type] + rel_time

        x_dict = self.gnn(
            x_dict,
            batch.edge_index_dict,
        )
        return x_dict

    def forward(
        self,
        batch: HeteroData,
        entity_table: NodeType,
        dst_table: NodeType,
    ) -> Tensor:
        seed_time = batch[entity_table].seed_time
        x_dict = self.forward_gnn(batch, entity_table)

        batch_size = seed_time.size(0)
        lhs_embedding = x_dict[entity_table][:
                                             batch_size]  # batch_size, channel
        lhs_embedding_projected = self.lhs_projector(lhs_embedding)
        rhs_gnn_embedding = x_dict[dst_table]  # num_sampled_rhs, channel
        rhs_idgnn_index = batch.n_id_dict[dst_table]  # num_sampled_rhs
        lhs_idgnn_batch = batch.batch_dict[dst_table]  # batch_size

        rhs_embedding = self.rhs_embedding()  # num_rhs_nodes, channel
        embgnn_logits = self.construct_logits(lhs_embedding_projected,
                                              lhs_embedding, rhs_gnn_embedding,
                                              rhs_embedding, lhs_idgnn_batch,
                                              rhs_idgnn_index)
        return embgnn_logits

    def forward_sample_softmax(
        self,
        batch: HeteroData,
        entity_table: NodeType,
        dst_table: NodeType,
        src_batch: Optional[Tensor] = None,
        dst_index: Optional[Tensor] = None,
    ) -> Tuple[Tensor, Tensor, Tensor]:
        r"""Forward function with RHS sample softmax."""
        seed_time = batch[entity_table].seed_time
        x_dict = self.forward_gnn(batch, entity_table)

        batch_size = seed_time.size(0)
        lhs_embedding = x_dict[entity_table][:
                                             batch_size]  # batch_size, channel
        lhs_embedding_projected = self.lhs_projector(lhs_embedding)
        rhs_gnn_embedding = x_dict[dst_table]  # num_sampled_rhs, channel
        rhs_idgnn_index = batch.n_id_dict[dst_table]  # num_sampled_rhs
        lhs_idgnn_batch = batch.batch_dict[dst_table]  # batch_size

        (rhs_idgnn_index, rhs_embedding, lhs_idgnn_batch, rhs_gnn_embedding,
         lhs_y_batch, rhs_y_index) = self.sample_step(rhs_idgnn_index,
                                                      lhs_idgnn_batch,
                                                      rhs_gnn_embedding,
                                                      src_batch, dst_index)
        embgnn_logits = self.construct_logits(lhs_embedding_projected,
                                              lhs_embedding, rhs_gnn_embedding,
                                              rhs_embedding, lhs_idgnn_batch,
                                              rhs_idgnn_index)
        return embgnn_logits, lhs_y_batch, rhs_y_index

    def to(self, *args, **kwargs) -> Self:
        return super().to(*args, **kwargs)

    def cpu(self) -> Self:
        return super().cpu()

    def cuda(self, *args, **kwargs) -> Self:
        return super().cuda(*args, **kwargs)
