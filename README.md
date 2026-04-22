## Legal Agent Demo

FastAPI + PostgreSQL demo for three linked flows:

- keyword retrieval
- case analysis from free-text events
- precedent-backed outcome prediction

### SQL

Use the current schema file:

```powershell
psql -U postgres -d legal_demo -f sql/legal_agent_demo.sql
```

### Environment

Create `.env` from `.env.example` and set at least:

```dotenv
DATABASE_URL=postgresql+psycopg2://postgres:123456@127.0.0.1:5432/legal_demo
CANLII_DATABASE_PAGES=https://www.canlii.org/en/on/onca/,https://www.canlii.org/en/on/onsc/
LLM_PROVIDER=spark
SPARK_API_KEY=your_api_key
SPARK_API_SECRET=your_api_secret
SPARK_APP_ID=your_app_id
SPARK_MODEL=Spark Ultra-32K
SPARK_DOMAIN=4.0Ultra
SPARK_BASE_URL=wss://spark-api.xf-yun.com/v4.0/chat
```

If model credentials are empty, retrieval and case analysis still work, and prediction falls back to preview mode.

### Run

Web app:

```powershell
cd d:\workcatlog\Pycharmproject\legal-demo
.\venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8020
```

Standalone ingestion worker:

```powershell
cd d:\workcatlog\Pycharmproject\legal-demo
.\venv\Scripts\python.exe -m app.worker
```

Then open:

```text
http://127.0.0.1:8020/
```

### Why The Worker Is Separate

The web process no longer consumes ingestion tasks. This avoids duplicate task execution under `--reload`, multiple uvicorn workers, or separate web replicas.

Current model:

1. the web process writes `ingestion_tasks`
2. the standalone worker claims tasks with `FOR UPDATE SKIP LOCKED`
3. the worker updates `last_heartbeat` while running
4. stale `running` tasks are re-queued automatically

### Async Hydration

When local results are below the requested `limit`, the app now:

1. returns current local results immediately
2. creates or reuses an `ingestion_tasks` row
3. lets the standalone worker fetch remote CanLII / OFAC data
4. writes new records into `source_items` and `item_keywords`
5. refreshes the page after polling sees task completion

Important settings:

- `ASYNC_HYDRATION_ENABLED`
- `INGESTION_WORKER_POLL_SECONDS`
- `INGESTION_WORKER_STALE_SECONDS`
- `INGESTION_TASK_REQUEUE_COOLDOWN_SECONDS`
- `INGESTION_WORKER_NAME`

### Demo Pages

```text
/           keyword search
/results    retrieval results
/analyze    case analysis
/predict    outcome prediction
```

### API Endpoints

```text
GET  /api/search?keywords=sanction&limit=10&source=all&sort=relevance
GET  /api/analyze-search?text=The+dispute+concerns+inheritance+and+property+division
GET  /api/predict?text=A+shareholder+claims+oppression+after+being+excluded+from+management
GET  /api/ingestion-tasks/{task_id}
POST /api/sync/ofac
POST /api/sync/canlii
POST /api/sync/all
```

### Current Boundaries

- CanLII data is still RSS-based, so coverage depends on the configured database pages and discovered database index pages
- OFAC is handled as sanctions data, not case law
- prediction quality depends on the local corpus and the configured model
- this is a research demo, not legal advice
