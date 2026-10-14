import uvicorn

from bucketeer.config import load_settings
from bucketeer.main import create_app


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


if __name__ == "__main__":
    main()
