import sys

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
    if "--fetch-ldap-ca" in sys.argv:
        from bucketeer.ldapca import main as fetch_ca

        raise SystemExit(fetch_ca(sys.argv[1:]))
    main()
