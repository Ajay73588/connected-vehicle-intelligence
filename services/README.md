# services/

All Python services share one library package (`src/cvi`) and one container image; the service is selected by the command. This keeps
the algorithms, schemas and storage adapters in a single, tested codebase while still deploying as independently scalable processes:

| service | command | module |
|---|---|---|
| API (+ agent) | `python -m cvi.api` | `src/cvi/api`, `src/cvi/agent` |
| ingestion edge (HTTP + MQTT) | `python -m cvi.ingestion.gateway` | `src/cvi/ingestion/gateway.py` |
| validator | `python -m cvi.ingestion.validator` | `src/cvi/ingestion/validator.py` |
| stream processor | `python -m cvi.stream.worker` | `src/cvi/stream` |
| lifecycle / batch ML scoring | `python -m cvi.lifecycle` | `src/cvi/lifecycle` |
| simulator | `python services/simulator/simulator.py …` | `src/cvi/simulator` |
| embedded all-in-one dev mode | `python -m cvi.local` | `src/cvi/local.py` |
