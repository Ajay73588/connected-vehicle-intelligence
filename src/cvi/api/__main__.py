import uvicorn

from cvi.common.config import get_settings

if __name__ == "__main__":
    s = get_settings()
    HOST = "0.0.0.0"  # nosec B104 - container entrypoint; exposure is controlled by the orchestrator / network policy
    uvicorn.run("cvi.api.app:create_app", factory=True, host=HOST, port=s.api_port, log_level="warning", proxy_headers=True, workers=1)
