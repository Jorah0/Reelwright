"""Custom-model training: a local "specialist" that learns the creator's clip judgment, with Claude as its teacher.

dataset.py builds SFT/DPO data from reviewed decisions; design.py is the model designer's arithmetic (no torch);
runner.py owns the external training env, the GPU lease and the worker subprocess; release.py evaluates, promotes and
rolls back; worker.py runs inside the training env (torch/transformers/peft live there, never in the .exe).
"""
