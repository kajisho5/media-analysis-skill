"""Convert the published YAMNet Keras weights (yamnet.h5, Apache-2.0) into the ONNX file this package ships
(src/media_analysis/models/yamnet_patches.onnx). Developer tool only: it is not part of the package and needs h5py and
onnx, which the package itself never imports.

    python tools/export_yamnet_onnx.py yamnet.h5 src/media_analysis/models/yamnet_patches.onnx

The graph takes log-mel patches [N, 96, 64] (computed by media_analysis.analyzers.events.log_mel_patches, the same
features as the reference feature code) and returns the 521 class scores [N, 521] (sigmoid). Batch-norm layers
(centre only, no scale, epsilon 1e-4) are folded into the convolution before them; Keras 'same' padding becomes
explicit pads (stride 2 on an even size pads one row / column at the end). Weights are stored as float16 and cast
to float32 once when the session is built, which halves the file; ADR-036 records the effect on the scores.

The layer table below follows the reference model definition; the weights and that definition are Apache-2.0
(src/media_analysis/models/yamnet.LICENSE)."""
from __future__ import annotations

import sys

import h5py
import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper

LAYERS = [("conv", 2, 32), ("sep", 1, 64), ("sep", 2, 128), ("sep", 1, 128), ("sep", 2, 256), ("sep", 1, 256), ("sep", 2, 512),
          ("sep", 1, 512), ("sep", 1, 512), ("sep", 1, 512), ("sep", 1, 512), ("sep", 1, 512), ("sep", 2, 1024), ("sep", 1, 1024)]
EPS = 1e-4


def main(h5_path: str, out_path: str) -> None:
    f = h5py.File(h5_path, "r")

    def w(path: str) -> np.ndarray:
        return np.asarray(f[path], dtype=np.float32)

    nodes, inits = [], []

    def const(name: str, arr: np.ndarray) -> str:
        inits.append(numpy_helper.from_array(arr.astype(np.float16), name + "_f16"))
        nodes.append(helper.make_node("Cast", [name + "_f16"], [name], to=TensorProto.FLOAT))
        return name

    def bn_fold(prefix: str, kernel: np.ndarray):
        beta, mean, var = (w(f"{prefix}/bn/{prefix}/bn/{k}:0") for k in ("beta", "moving_mean", "moving_variance"))
        scale = 1.0 / np.sqrt(var + EPS)
        return kernel * scale.reshape(-1, 1, 1, 1), beta - mean * scale

    inits.append(numpy_helper.from_array(np.array([-1, 1, 96, 64], dtype=np.int64), "shape"))
    nodes.append(helper.make_node("Reshape", ["patches", "shape"], ["x0"]))
    x, h, wd = "x0", 96, 64

    def conv(name, inp, kernel, bias, stride, group, size):
        hh, ww = size
        if kernel.shape[2] == 1:
            pads, out = [0, 0, 0, 0], (hh, ww)
        elif stride == 1:
            pads, out = [1, 1, 1, 1], (hh, ww)
        else:
            assert hh % 2 == 0 and ww % 2 == 0
            pads, out = [0, 0, 1, 1], (hh // 2, ww // 2)
        k, b = const(name + "_w", kernel), const(name + "_b", bias)
        nodes.append(helper.make_node("Conv", [inp, k, b], [name + "_c"], strides=[stride, stride], pads=pads, group=group,
                                      kernel_shape=list(kernel.shape[2:])))
        nodes.append(helper.make_node("Relu", [name + "_c"], [name]))
        return name, out

    for i, (kind, stride, filters) in enumerate(LAYERS, 1):
        if kind == "conv":
            k = w(f"layer{i}/conv/layer{i}/conv/kernel:0").transpose(3, 2, 0, 1)
            k, b = bn_fold(f"layer{i}/conv", k)
            x, (h, wd) = conv(f"l{i}", x, k, b, stride, 1, (h, wd))
        else:
            dk = w(f"layer{i}/depthwise_conv/layer{i}/depthwise_conv/depthwise_kernel:0").transpose(2, 3, 0, 1)
            dk, db = bn_fold(f"layer{i}/depthwise_conv", dk)
            x, (h, wd) = conv(f"l{i}d", x, dk, db, stride, dk.shape[0], (h, wd))
            pk = w(f"layer{i}/pointwise_conv/layer{i}/pointwise_conv/kernel:0").transpose(3, 2, 0, 1)
            pk, pb = bn_fold(f"layer{i}/pointwise_conv", pk)
            x, (h, wd) = conv(f"l{i}p", x, pk, pb, 1, 1, (h, wd))
    nodes.append(helper.make_node("GlobalAveragePool", [x], ["gap"]))
    nodes.append(helper.make_node("Flatten", ["gap"], ["emb"], axis=1))
    kw, kb = const("logits_w", w("logits/logits/kernel:0")), const("logits_b", w("logits/logits/bias:0"))
    nodes.append(helper.make_node("Gemm", ["emb", kw, kb], ["logits"]))
    nodes.append(helper.make_node("Sigmoid", ["logits"], ["scores"]))
    graph = helper.make_graph(nodes, "yamnet_patches",
                              [helper.make_tensor_value_info("patches", TensorProto.FLOAT, ["n", 96, 64])],
                              [helper.make_tensor_value_info("scores", TensorProto.FLOAT, ["n", 521])], inits)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)], producer_name="tools/export_yamnet_onnx.py")
    model.ir_version = 8
    model.doc_string = "YAMNet (Apache-2.0) class scores from 96x64 log-mel patches; weights float16, batch norm folded"
    onnx.checker.check_model(model)
    onnx.save(model, out_path)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
