## Legal Demo MVP

FastAPI + PostgreSQL legal search demo for OFAC sanctions data and CanLII case RSS data.

### Setup

1. Create the PostgreSQL database and tables:

```powershell
psql -U postgres -f sql/min_demo.sql
```

2. Check `.env`:

```dotenv
DATABASE_URL=postgresql+psycopg2://postgres:123456@127.0.0.1:5432/legal_demo
CANLII_DATABASE_PAGES=https://www.canlii.org/en/on/onca/,https://www.canlii.org/en/on/onsc/
```

OFAC CSV URLs can be left empty. The app will try discovery first and then fall back to the default OFAC legacy CSV URLs.

3. Start the app:

```powershell
.\venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### Useful Endpoints

```text
GET  /
GET  /results?keywords=sanction&limit=30&source=all&sort=relevance
GET  /api/search?keywords=sanction&limit=30&source=ofac&sort=relevance
POST /api/sync/ofac
POST /api/sync/canlii
POST /api/sync/all
```

Search parameters:

- `source`: `all`, `ofac`, or `canlii`
- `sort`: `relevance` or `recent`
- `limit`: `1` to `100`
- `offset`: pagination offset
