# Credits

## Inspiration

Anyaclaw's architecture (gateway + channels + provider + agent loop + tools) was shaped by an
earlier local-first, multi-channel agent project. No source files were copied: storage, security,
observability, packaging and error handling were redesigned. The concrete decisions are documented
in [docs/improvements.md](docs/improvements.md).

## Dependencies

* [httpx](https://www.python-httpx.org/) — HTTP client (MIT)
* [FastAPI](https://fastapi.tiangolo.com/) / [Starlette](https://www.starlette.io/) / [Uvicorn](https://www.uvicorn.org/) — web console (MIT/BSD)
* [python-docx](https://python-docx.readthedocs.io/), [python-pptx](https://python-pptx.readthedocs.io/),
  [openpyxl](https://openpyxl.readthedocs.io/), [ReportLab](https://www.reportlab.com/),
  [pypdf](https://pypdf.readthedocs.io/) — document tools (MIT/BSD)
* [Model Context Protocol SDK](https://github.com/modelcontextprotocol/python-sdk) — MCP client (MIT)

## Typeface & UI

The web console uses only system fonts (`system-ui`, `Segoe UI`, `PingFang SC`, `Microsoft YaHei`)
so it renders correctly offline and on every OS.
