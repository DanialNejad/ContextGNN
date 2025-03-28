## ContextGNN

This repository contains PyTorch implementation of ContextGNN.

## How to Run

To reproduce results on RelBench, run `benchmark/relbench_link_prediction_benchmark.py`.

```sh
python benchmark/relbench_link_prediction_benchmark.py --model contextgnn
```

To reproduce results on IJCAI-Contest, run `benchmark/tgt_ijcai_benchmark.py`.

```sh
python benchmark/tgt_ijcai_benchmark.py --model contextgnn
```

To run ContextGNN on any RelBench dataset, without tuning, run `examples/relbench_example.py`.

```sh
python examples/relbench_example.py --dataset rel-trial --task site-sponsor-run --model contextgnn
```

To run ContextGNN with sampled right hand side nodes, run `contextgnn_sample_softmax.py`.

```sh
python examples/contextgnn_sample_softmax.py --rhs_sample_size 1000
```

## Install Instruction

```sh
pip install -e .

# to run examples and benchmarks
pip install -e '.[full]'
```
