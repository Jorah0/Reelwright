"""Reelwright training worker: runs INSIDE the training env (torch, transformers, peft), never inside the .exe.

    python worker.py check | params --config C | train --config C | merge --config C --out DIR
                   | convert --script convert_hf_to_gguf.py --model DIR --outfile F --outtype f16

Every line on stdout is one JSON object with an "event": step / params / tokenizer / checkpoint / paused / resumed /
canceled / done / error (anything else a library prints is shown as a log line). Training checkpoints at step
boundaries (weights or adapter, optimizer, scheduler, RNG, data cursor) and resumes from the last one. A "pause" file in
$REELWRIGHT_CONTROL pauses at the next step boundary (after a checkpoint); a "cancel" file checkpoints and exits 3.
Self-contained on purpose: it imports nothing from reelwright.
"""
import argparse
import hashlib
import json
import math
import os
import random
import sys
import time
from pathlib import Path

SPECIALS = ["<|end|>", "<|system|>", "<|user|>", "<|assistant|>", "<|pad|>"]


def is_lora(mode: str) -> bool:
    return mode in ("lora", "qlora")  # qlora = LoRA on a 4-bit NF4 base (the recipe carries quant)


def emit(event: str, **kw):
    print(json.dumps({"event": event, **kw}), flush=True)


def control(name: str) -> bool:
    d = os.environ.get("REELWRIGHT_CONTROL")
    return bool(d) and (Path(d) / name).exists()


def llama_config(a: dict, **ids):
    from transformers import LlamaConfig
    return LlamaConfig(vocab_size=a["vocab"], hidden_size=a["width"], intermediate_size=a["mlp"],
                       num_hidden_layers=a["layers"], num_attention_heads=a["heads"], num_key_value_heads=a["kv_heads"],
                       max_position_embeddings=a["context"], tie_word_embeddings=a["tied"], rms_norm_eps=1e-5,
                       rope_theta=10000.0, attention_bias=False, mlp_bias=False, **ids)


def cmd_check(_):
    import torch
    out = {"torch": torch.__version__, "cuda": torch.cuda.is_available(), "cuda_version": torch.version.cuda}
    if out["cuda"]:
        out.update(gpu=torch.cuda.get_device_name(0), vram_gb=round(torch.cuda.get_device_properties(0).total_memory / 2**30, 1),
                   bf16=torch.cuda.is_bf16_supported())
    for mod in ("transformers", "peft", "tokenizers", "accelerate"):
        try:
            out[mod] = __import__(mod).__version__
        except ImportError:
            out[mod] = None
    emit("check", **out)


def cmd_params(cfg):
    import torch
    from transformers import LlamaForCausalLM
    with torch.device("meta"):
        model = LlamaForCausalLM(llama_config(cfg["arch"]))
    emit("params", count=sum(p.numel() for p in model.parameters()))  # tied weights are one parameter


# ---------- data ----------

def read_jsonl(path) -> list[dict]:
    p = Path(path) if path else None
    return [json.loads(x) for x in p.read_text("utf-8").splitlines() if x.strip()] if p and p.is_file() else []


def chat_text(messages: list[dict]) -> tuple[str, str]:
    """Reelwright's own chat format (scratch models): the prompt, and the target ending in <|end|>."""
    prompt = "".join(f"<|{m['role']}|>\n{m['content']}\n" for m in messages[:-1]) + "<|assistant|>\n"
    return prompt, messages[-1]["content"] + "<|end|>"


def scratch_tokenizer(cfg, rows, out: Path):
    from tokenizers import Tokenizer, decoders, models, pre_tokenizers, trainers
    from transformers import PreTrainedTokenizerFast
    name = cfg["arch"].get("tokenizer") or "bpe"
    if name != "bpe":  # a pinned tokenizer (repo@revision) reused as is, with the chat tokens added
        tok = PreTrainedTokenizerFast.from_pretrained(name, revision=cfg["arch"].get("tokenizer_revision") or "main")
        tok.add_special_tokens({"additional_special_tokens": SPECIALS})
    else:
        t = Tokenizer(models.BPE())
        t.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
        t.decoder = decoders.ByteLevel()
        trainer = trainers.BpeTrainer(vocab_size=cfg["arch"]["vocab"], special_tokens=SPECIALS, show_progress=False,
                                      initial_alphabet=pre_tokenizers.ByteLevel.alphabet())
        texts = [m["content"] for r in rows for m in r["messages"]]
        t.train_from_iterator(texts, trainer=trainer)
        tok = PreTrainedTokenizerFast(tokenizer_object=t, eos_token="<|end|>", pad_token="<|pad|>",
                                      additional_special_tokens=SPECIALS[1:4])
        (out / "reelwright_tokenizer.json").write_text(json.dumps({"pre": "gpt-2"}), "utf-8")  # read by `convert`
    emit("tokenizer", vocab=len(tok), requested=cfg["arch"]["vocab"])
    return tok


def blocks(tok, rows, seq: int) -> list[list[int]]:
    """Scratch: every example as text, joined by <|end|>, cut into seq-token blocks (the LM learns the whole format)."""
    ids = []
    for r in rows:
        p, t = chat_text(r["messages"])
        ids += tok(p + t, add_special_tokens=False)["input_ids"]
    if len(ids) < seq + 1:
        ids = ids * math.ceil((seq + 1) / max(1, len(ids)))  # a tiny pilot set still fills one block
    return [ids[i:i + seq + 1] for i in range(0, len(ids) - seq, seq)]


def sft_items(tok, rows, seq: int) -> list[tuple[list[int], list[int]]]:
    """LoRA: (input ids, labels) per example; loss only on the target; the prompt is cut from the left to fit."""
    out = []
    for r in rows:
        prompt = tok.apply_chat_template(r["messages"][:-1], add_generation_prompt=True, tokenize=False)
        p = tok(prompt, add_special_tokens=False)["input_ids"]
        t = tok(r["messages"][-1]["content"] + (tok.eos_token or ""), add_special_tokens=False)["input_ids"][:seq // 2]
        p = p[-(seq - len(t)):]
        out.append((p + t, [-100] * len(p) + t))
    return out


# ---------- training ----------

def rng_state():
    import numpy as np
    import torch
    name, keys, pos, gauss, cached = np.random.get_state()  # plain types: the checkpoint loads with weights_only=True
    return {"py": list(random.getstate()[1]) + [random.getstate()[0]], "np": [name, keys.tolist(), pos, gauss, cached],
            "torch": torch.get_rng_state(),
            "cuda": torch.cuda.get_rng_state_all() if torch.cuda.is_available() else None}


def set_rng(s):
    import numpy as np
    import torch
    random.setstate((s["py"][-1], tuple(s["py"][:-1]), None))
    np.random.set_state((s["np"][0], np.array(s["np"][1], dtype=np.uint32), *s["np"][2:]))
    torch.set_rng_state(s["torch"])
    if s.get("cuda") is not None and torch.cuda.is_available():
        torch.cuda.set_rng_state_all(s["cuda"])


def cmd_train(cfg):
    import torch
    out = Path(cfg["out"])
    out.mkdir(parents=True, exist_ok=True)
    tr = cfg["train"]
    seed = int(cfg.get("arch", {}).get("seed") or tr.get("seed") or 1234)
    random.seed(seed)
    torch.manual_seed(seed)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    rows = read_jsonl(cfg["data"]["train"])
    if not rows:
        raise RuntimeError("The training split is empty.")
    lora = is_lora(cfg["mode"])
    seq, micro, accum = int(tr["seq"]), int(tr["micro"]), int(tr["accum"])
    if lora:
        model, tok = load_lora(cfg, dev)
        items = sft_items(tok, rows, seq)
        steps = int(tr.get("max_steps") or math.ceil(len(items) * int(cfg["recipe"]["epochs"]) / (micro * accum)))
    else:
        from transformers import LlamaForCausalLM
        tok = scratch_tokenizer(cfg, rows, out)
        items = blocks(tok, rows, seq)
        model = LlamaForCausalLM(llama_config(cfg["arch"], eos_token_id=tok.eos_token_id, pad_token_id=tok.pad_token_id)).to(dev)
        n = sum(p.numel() for p in model.parameters())
        emit("params", count=n)
        steps = int(tr.get("max_steps") or math.ceil(int(tr["tokens"]) / (seq * micro * accum)))
    params = [p for p in model.parameters() if p.requires_grad]
    lr, warm = float(tr["lr"]), max(1, int(steps * float(tr.get("warmup", 0.02))))
    opt = torch.optim.AdamW(params, lr=lr, betas=(0.9, 0.95), weight_decay=0.0 if lora else 0.1, fused=dev == "cuda")
    sched = torch.optim.lr_scheduler.LambdaLR(
        opt, lambda s: (s + 1) / warm if s < warm else 0.1 + 0.9 * 0.5 * (1 + math.cos(math.pi * (s - warm) / max(1, steps - warm))))
    ckpt, step, cursor = out / "checkpoint.pt", 0, 0
    if ckpt.is_file():  # resume exactly: weights/adapter, optimizer, scheduler, RNG, data cursor
        st = torch.load(ckpt, map_location=dev, weights_only=True)
        model.load_state_dict(st["model"], strict=not lora)
        opt.load_state_dict(st["opt"])
        sched.load_state_dict(st["sched"])
        set_rng(st["rng"])
        step, cursor = st["step"], st["cursor"]
        emit("resumed", step=step)

    def save():
        sd = {k: v for k, v in model.state_dict().items() if not lora or "lora_" in k}
        torch.save({"model": sd, "opt": opt.state_dict(), "sched": sched.state_dict(), "rng": rng_state(), "step": step,
                    "cursor": cursor}, out / "checkpoint.tmp")
        os.replace(out / "checkpoint.tmp", ckpt)
        emit("checkpoint", step=step)

    def batch(k):
        epoch, i = divmod(k, len(items))
        order = list(range(len(items)))
        random.Random(seed + epoch).shuffle(order)  # a fixed order per epoch: the cursor alone resumes it
        return items[order[i]]

    if dev == "cuda":
        torch.cuda.reset_peak_memory_stats()
    model.train()
    save_every, t0, tok_count, timed_from = int(tr.get("save_every") or 200), None, 0, 2
    last_emit = 0.0
    while step < steps:
        if control("cancel"):
            save()
            emit("canceled", step=step)
            sys.exit(3)
        if control("pause"):
            save()
            emit("paused", step=step)
            while control("pause") and not control("cancel"):
                time.sleep(1)
            if not control("cancel"):
                emit("resumed", step=step)
            continue
        if step == timed_from and dev == "cuda":
            torch.cuda.synchronize()
        if step == timed_from:
            t0, tok_count = time.perf_counter(), 0
        total = 0.0
        for _ in range(accum):
            group = [batch(cursor + j) for j in range(micro)]
            cursor += micro
            if lora:
                width = max(len(x) for x, _ in group)
                ids = torch.tensor([x + [tok.pad_token_id or 0] * (width - len(x)) for x, _ in group], device=dev)
                labels = torch.tensor([y + [-100] * (width - len(y)) for _, y in group], device=dev)
                mask = torch.tensor([[1] * len(x) + [0] * (width - len(x)) for x, _ in group], device=dev)
            else:
                ids = torch.tensor([b[:-1] for b in group], device=dev)
                labels, mask = ids, None
            with torch.autocast(dev, dtype=torch.bfloat16, enabled=dev == "cuda"):
                loss = model(input_ids=ids, attention_mask=mask, labels=labels).loss / accum
            loss.backward()
            total += loss.item()
            tok_count += int(ids.numel())
        torch.nn.utils.clip_grad_norm_(params, 1.0)
        opt.step()
        sched.step()
        opt.zero_grad(set_to_none=True)
        step += 1
        if dev == "cuda":
            torch.cuda.synchronize()
        tok_s = tok_count / (time.perf_counter() - t0) if t0 and step > timed_from else None
        if time.time() - last_emit > 0.5 or step == steps:
            emit("step", step=step, of=steps, loss=round(total, 4), tok_s=round(tok_s) if tok_s else None, lr=sched.get_last_lr()[0])
            last_emit = time.time()
        if step % save_every == 0 and step < steps:
            save()
    save()
    final = out / ("adapter" if lora else "merged")
    model.save_pretrained(final, safe_serialization=True)
    tok.save_pretrained(final)
    if not lora and (out / "reelwright_tokenizer.json").is_file():
        (final / "reelwright_tokenizer.json").write_bytes((out / "reelwright_tokenizer.json").read_bytes())
    emit("done", steps=step, tok_s=round(tok_s) if tok_s else None, tokens_per_update=seq * micro * accum,
         peak_vram_gb=round(torch.cuda.max_memory_allocated() / 2**30, 3) if dev == "cuda" else None, model=str(final),
         device=dev)


def load_lora(cfg, dev):
    import torch
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    b, r = cfg["base"], cfg["recipe"]
    tok = AutoTokenizer.from_pretrained(b["repo"], revision=b["revision"])
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    q = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16,
                           bnb_4bit_use_double_quant=True) if r.get("quant") == "nf4" else None
    model = AutoModelForCausalLM.from_pretrained(b["repo"], revision=b["revision"], quantization_config=q,
                                                 dtype=torch.bfloat16, device_map={"": 0} if dev == "cuda" else None)
    if q:
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    model = get_peft_model(model, LoraConfig(r=int(r["r"]), lora_alpha=int(r["alpha"]), lora_dropout=float(r["dropout"]),
                                             target_modules="all-linear", task_type="CAUSAL_LM"))
    emit("params", count=sum(p.numel() for p in model.parameters()),
         trainable=sum(p.numel() for p in model.parameters() if p.requires_grad))
    return model, tok


def cmd_merge(cfg, out):
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer
    b = cfg["base"]
    base = AutoModelForCausalLM.from_pretrained(b["repo"], revision=b["revision"], dtype=torch.bfloat16)
    model = PeftModel.from_pretrained(base, str(Path(cfg["out"]) / "adapter")).merge_and_unload()
    model.save_pretrained(out, safe_serialization=True)
    AutoTokenizer.from_pretrained(str(Path(cfg["out"]) / "adapter")).save_pretrained(out)
    emit("done", merged=str(out))


def cmd_convert(a):
    """Run llama.cpp's pinned convert_hf_to_gguf.py in-process. A Reelwright-trained byte-level BPE is unknown to its
    pre-tokenizer table (it fingerprints known tokenizers), so ours is declared "gpt-2", which is what it is."""
    import importlib.util
    script = Path(a.script)
    sys.path[:0] = [str(script.parent), str(script.parent / "gguf-py")]  # its own "conversion" package and gguf-py
    spec = importlib.util.spec_from_file_location("convert_hf_to_gguf", script)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["convert_hf_to_gguf"] = mod
    spec.loader.exec_module(mod)
    marker = Path(a.model) / "reelwright_tokenizer.json"
    if marker.is_file():
        pre = json.loads(marker.read_text("utf-8"))["pre"]
        for m in [mod, *(v for k, v in list(sys.modules.items()) if k == "conversion" or k.startswith("conversion."))]:
            for cls in vars(m).values():
                if isinstance(cls, type) and "get_vocab_base_pre" in vars(cls):
                    cls.get_vocab_base_pre = lambda self, tokenizer, _p=pre: _p
    sys.argv = [str(script), str(a.model), "--outfile", str(a.outfile), "--outtype", a.outtype]
    mod.main()
    h = hashlib.sha256()
    with open(a.outfile, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    emit("done", gguf=str(a.outfile), sha256=h.hexdigest(), bytes=Path(a.outfile).stat().st_size)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["check", "params", "train", "merge", "convert"])
    ap.add_argument("--config")
    ap.add_argument("--out")
    ap.add_argument("--script")
    ap.add_argument("--model")
    ap.add_argument("--outfile")
    ap.add_argument("--outtype", default="f16")
    a = ap.parse_args()
    cfg = json.loads(Path(a.config).read_text("utf-8")) if a.config else {}
    try:
        if a.mode == "check":
            cmd_check(cfg)
        elif a.mode == "params":
            cmd_params(cfg)
        elif a.mode == "train":
            cmd_train(cfg)
        elif a.mode == "merge":
            cmd_merge(cfg, a.out)
        else:
            cmd_convert(a)
    except SystemExit:
        raise
    except Exception as e:
        emit("error", message=f"{type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    main()
