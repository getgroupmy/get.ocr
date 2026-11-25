"""How models/jawi/jawi.onnx was made. Not needed to run the service: the converted file is in
the repository. Run it only to make the file again, on a machine with PyTorch:

    pip install "kraken>=6,<7" onnx onnxruntime
    curl -L -o jawi.mlmodel https://huggingface.co/budaya-lab/Jawi-OCR-Kraken-v1/resolve/main/real_plus_synth_200_best_250326.mlmodel
    python scripts/convert_jawi_model.py jawi.mlmodel models/jawi
"""
import json
import sys

import numpy as np
import onnxruntime as ort
import torch
from kraken.lib import models

source, folder = sys.argv[1], sys.argv[2]
loaded = models.load_any(source, device="cpu").nn
net = loaded.nn.eval()


class Scores(torch.nn.Module):
    """The network without Kraken's wrapper: a line picture in, a score for every letter at every step out."""

    def __init__(self, inner: torch.nn.Module) -> None:
        super().__init__()
        self.inner = inner

    def forward(self, line: torch.Tensor) -> torch.Tensor:
        return self.inner(line)[0]


example = torch.rand(1, 1, 120, 600)
torch.onnx.export(Scores(net).eval(), (example,), f"{folder}/jawi.onnx", input_names=["line"], output_names=["probs"],
                  dynamic_axes={"line": {3: "w"}, "probs": {3: "t"}}, opset_version=17, dynamo=False)
assert all(len(label) == 1 for label in loaded.codec.c2l.values())
with open(f"{folder}/labels.json", "w", encoding="utf-8") as out:
    json.dump({str(label[0]): char for char, label in loaded.codec.c2l.items()}, out, ensure_ascii=False)

session = ort.InferenceSession(f"{folder}/jawi.onnx")
for width in (97, 200, 600, 1733, 3000):
    line = torch.rand(1, 1, 120, width)
    with torch.no_grad():
        want = net(line)[0].numpy()
    got = session.run(None, {"line": line.numpy()})[0]
    print(width, "largest difference", float(np.abs(want - got).max()))
