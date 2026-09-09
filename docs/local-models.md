# Downloadable local models

Settings → AI models → On this computer and the setup wizard's Models step
offer the same catalog. Select a model, check the RAM and disk guidance, then
choose **Download and use model**. Downloaded models can be selected again
offline with **Use selected model**. The bundled starter remains the default.

| Model | Download | Total RAM guidance | Use |
| --- | --- | --- | --- |
| SmolLM2 135M | 101 MiB, bundled | 1 GiB | Basic chat; unreliable for tools |
| Qwen3 1.7B Q4_K_M | 1.2 GiB | 4 GiB | Smaller tool-capable option |
| Qwen3 4B Q4_K_M | 2.3 GiB | 8 GiB | Mid-size tool-capable option |
| Qwen3 8B Q4_K_M | 4.7 GiB | 12 GiB | Larger tool-capable option |

RAM figures are AIOS planning estimates for CPU inference with an 8,192-token
context and desktop overhead, not benchmarked guarantees. Larger models run
more slowly on CPU. Other apps and browser pages consume additional RAM.
AIOS checks total guest RAM (allowing 5% for kernel reservations) and free disk
space before downloading or selecting a model. It reserves an extra 512 MiB of
disk space for new downloads. If RAM detection is unavailable, the picker says
so and allows a manual choice. Close other apps if memory is tight; refresh
after changing resources. In a VM, increase `AIOS_VM_MEM_MB` before booting;
host RAM does not count as guest RAM. Live-session downloads may be temporary.

All entries use Apache-2.0 licensed models. Qwen's model cards document tool
capabilities: [1.7B](https://huggingface.co/Qwen/Qwen3-1.7B),
[4B](https://huggingface.co/Qwen/Qwen3-4B), and
[8B](https://huggingface.co/Qwen/Qwen3-8B). The Q4_K_M GGUF files come from
[Bartowski's 1.7B](https://huggingface.co/bartowski/Qwen_Qwen3-1.7B-GGUF),
[4B](https://huggingface.co/bartowski/Qwen_Qwen3-4B-GGUF), and
[8B](https://huggingface.co/bartowski/Qwen_Qwen3-8B-GGUF) repositories.
`apps/aios/models.json` pins each revision, byte size, and LFS SHA-256 checksum,
verified against Hugging Face metadata on 2026-09-09. Downloads and reused files
are checksum-verified before switching the saved configuration. Cancelled or
failed downloads leave the previous model selected. A damaged saved model must
be removed before downloading it again.

Both desktop and CLI servers enable llama.cpp's Jinja tool templates and disable
Qwen3 thinking mode to keep local responses within the desktop context budget.
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
