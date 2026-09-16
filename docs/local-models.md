# Downloadable local models

Settings → AI models → On this computer and the setup wizard's Models step
offer the same Qwen-only catalog. Select a model, check the RAM and disk guidance,
then choose **Download and use model**. Downloaded models can be selected again
offline with **Use selected model**. Qwen3 0.6B is bundled as the offline default.

| Model | Download | Total RAM guidance | Use |
| --- | --- | --- | --- |
| Qwen3 0.6B Q4_K_M | 462 MiB, bundled | 2 GiB | Basic offline chat and simple tool bootstrap |
| Qwen3 1.7B Q4_K_M | 1.2 GiB | 4 GiB | Lightweight upgrade for chat and basic tools |
| Qwen3 4B Q4_K_M | 2.3 GiB | 8 GiB | Balanced chat and browser-tool option |
| Qwen3 8B Q4_K_M | 4.7 GiB | 12 GiB | Strongest curated local option |

RAM figures are AIOS planning estimates for CPU inference with an 8,192-token
context and desktop overhead, not benchmarked guarantees. Larger models run
more slowly on CPU. Other apps and browser pages consume additional RAM.
AIOS checks total guest RAM (allowing 5% for kernel reservations) and free disk
space before downloading or selecting a model. It reserves an extra 512 MiB of
disk space for new downloads. If RAM detection is unavailable, the picker says
so and allows a manual choice. Close other apps if memory is tight; refresh
after changing resources. In a VM, increase `AIOS_VM_MEM_MB` before booting;
host RAM does not count as guest RAM. The launchers default to 16 GiB because
live-session storage is RAM-backed; downloads are temporary until AIOS is
installed.

The bundled inference binaries are built for x86_64 CPUs with SSE4.2, AVX,
AVX2, BMI2, F16C and FMA. AIOS checks those instruction sets before starting
`llama-server` or `whisper-cli`: on a CPU without them the catalog reports
every model as unavailable with the missing features, downloads are refused,
and chat says local models cannot run here. The desktop and remote or
subscription providers keep working, and no download is started that could not
be used. See [recovery and diagnostics](hardware-recovery.md).

All entries use Apache-2.0 licensed Qwen models. Qwen's model cards document tool
capabilities: [0.6B](https://huggingface.co/Qwen/Qwen3-0.6B),
[1.7B](https://huggingface.co/Qwen/Qwen3-1.7B),
[4B](https://huggingface.co/Qwen/Qwen3-4B), and
[8B](https://huggingface.co/Qwen/Qwen3-8B). The Q4_K_M GGUF files come from
[Bartowski's 0.6B](https://huggingface.co/bartowski/Qwen_Qwen3-0.6B-GGUF),
[1.7B](https://huggingface.co/bartowski/Qwen_Qwen3-1.7B-GGUF),
[4B](https://huggingface.co/bartowski/Qwen_Qwen3-4B-GGUF), and
[8B](https://huggingface.co/bartowski/Qwen_Qwen3-8B-GGUF) repositories.
`apps/aios/models.json` pins each repository, revision, filename, byte size, and
LFS SHA-256 checksum,
verified against Hugging Face metadata on 2026-09-09. Downloads and reused files
are checksum-verified before switching the saved configuration. Cancelled or
failed downloads leave the previous model selected. A damaged saved model must
be removed before downloading it again.

Both desktop and CLI servers enable llama.cpp's Jinja tool templates and disable
Qwen3 thinking mode to keep starter and tool responses within the desktop context
budget. Setup, model installation, network, and power controls use deterministic
backend actions rather than asking a local model to generate critical commands.
The pinned llama.cpp runtime supports these models. See
[llama.cpp function calling](https://github.com/ggml-org/llama.cpp/blob/5266f24da75dc449bd56cbed7addb9c8e4a6a73e/docs/function-calling.md).
Tool capability does not guarantee reliable completion of every browser task.
AIOS's existing bounded browser tool loop remains responsible for execution.

```sh
aios-llm local-models             # Catalog, requirements, and availability
aios-llm setup-local qwen3-4b     # Verify, download if needed, and select
aios-llm serve                   # Start the selected model for CLI use
aios-llm setup-local             # Return to the starter model
```

Manual GGUF imports and remote providers remain available in Settings.
