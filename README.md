# WhatsApp tools

Read and send WhatsApp Web messages through a dedicated Chrome profile. The Mac default is
`~/Library/Application Support/iNBest/WhatsApp`. Set `WA_PROFILE` or pass `--profile` to
select another account.

Install from `main` with `uv tool install git+https://github.com/luiscosio/whatsapp-tools.git@main`.
This installs `wa` and `inbest-wa`. Schedulers can invoke the absolute executable path from
`uv tool dir --bin`. A project adapter passes its destination to `inbest-wa request-v1`
as JSON on standard input. Live sends also require `--yes`.

```sh
wa login
wa check
wa read --chat "Exact group title" --since 2026-11-23
wa files --chat "Exact group title" --since 2026-11-23
wa send --chat "Exact group title" --file message.md
```

The send command previews the message. Add `--yes` to send approved text. Use `--media` for an
image or video. The tool stops after an unconfirmed send. Check the conversation before retrying.

Live jobs run headlessly by default. QR linking opens a dedicated window. On macOS both modes
use the same Chrome executable and profile. A filesystem lock serializes jobs. Routine jobs reuse one detached headless process per profile and close their own tabs.
An explicit QR login can switch that worker to a visible window. Close any manually opened
WhatsApp Chrome window when you want the headless worker to resume. Store downloads outside source control.

## Development

Use `uv sync --locked`, `uv run pytest`, and `uv run ruff check src tests`.
The fixture suite runs offline and starts no browser. For a read-only live check, run `wa check`.
