# product-search-hybrid-api — Design

## 0. 設計總綱

- **Simplicity First**：本次只交付「核心 hybrid search API」最小集合。不做 filter、分頁、降權、快取、降級重試 —— 等真實需求。
- **重用不重寫**：k-NN / BM25 查詢 DSL 與 query embedding 邏輯自 `scripts/etl/verify_search_os.py` 的可重用函式 **lift 進 search 模組改 async**（該腳本 docstring 已預告此事）。golden set 與 LLM-judge 量尺直接沿用 Phase 1 產物。
- **演算法處理融合、LLM 只做 embedding**：RRF 是純 Python 公式（`Σ 1/(k+rank)`），不讓 OpenSearch pipeline 或 LLM 介入排序。Bedrock 在本鏈路只做一件原子事：query embedding。
- **mock 預設**：`analyzer_mock_mode=true`（既有預設）下 `/search` 端到端可跑、零 Bedrock 呼叫、零花費。真 embedding 是 opt-in。
- **零 migration**：search 的儲存層是 OpenSearch，PostgreSQL schema 一個字都不碰。

## 1. 領域模組 `src/recommender/search/` — 結構與職責

### 1.1 為什麼是領域模組（既有「層優先」codebase 的刻意例外）

計劃文件 §Phase 2 已拍板：search 的基礎設施（OpenSearch）與核心 Postgres + Bedrock 鏈完全不同，是 codebase **第一個 infra 斷層 domain**。把 repository/service/router 散進 `repositories/` `services/` `api/` 會讓「誰依賴 OpenSearch」的邊界模糊；圈成 `search/` 自含模組才能解耦、易抽換/獨立部署。

**與既有架構的共存方式**（領域模組不是平行宇宙，三層紀律與橫切設施照舊）：

- 模組**內部**仍是 router → service → repository 三層，職責對齊 coding-rules（router 不碰 client、service 不回 raw hit、repository 純 I/O 無業務判斷）。
- **DI wiring 仍只在 `deps.py`**（唯一 wiring 點不破例，見 §9 取捨）。
- **錯誤處理走既有橫切**：search 模組不自拋 `HTTPException`；查無結果是正常業務結果（回空 list），不是 `NotFoundError`；OpenSearch 連線失敗等未預期錯誤直接往上飄給 `main.py` 的全域 Exception handler 轉 500。
- **lifespan 整合進 `main.py`** 既有 startup/shutdown 流程，不另起進程或背景常駐。

### 1.2 檔案結構

```
src/recommender/search/
├── __init__.py
├── schemas.py       # Pydantic DTO：SearchResultItem / SearchResponse
├── embeddings.py    # @lru_cache get_bedrock_embeddings(...)（比照 llm.py）+ MOCK_QUERY_VECTOR
├── client.py        # @lru_cache get_opensearch_client() + async close_opensearch_client()
├── repository.py    # SearchRepository：build_knn_body/build_bm25_body 純函式 + hybrid_msearch I/O
├── rrf.py           # reciprocal_rank_fusion 純函式
├── service.py       # SearchService：embed → msearch → RRF → DTO
└── router.py        # GET /search
```

| 檔案 | 職責 | 不做什麼 |
|------|------|---------|
| `schemas.py` | `SearchResultItem`（`mart_id` / `mart_name` / `score` / `brand` / `price` / `category` 後三者 optional）、`SearchResponse`（`query` + `results: list[SearchResultItem]`） | 不放 OpenSearch hit 結構（那是 repository 內部事） |
| `embeddings.py` | `get_bedrock_embeddings(model_id, region, profile, dimensions)` 回 cached `BedrockEmbeddings`；`MOCK_QUERY_VECTOR` 常數 | 不判斷 mock（那是 service 的事）；不放 chat model（那是 `llm.py`） |
| `client.py` | AsyncOpenSearch 單例的建構與關閉 | 不發查詢 |
| `repository.py` | DSL 建構（純函式）+ `msearch` 呼叫（I/O），回兩組 raw hits | 不讀 `settings`（host/index 由建構子注入）；不做融合、不做 DTO 轉換 |
| `rrf.py` | 純函式排名融合 | 零 import OpenSearch / Pydantic 型別 |
| `service.py` | 編排：mock 判斷、embed、候選數計算、融合、hit→DTO 映射 | 不組 DSL、不碰 HTTP |
| `router.py` | 參數驗證、呼叫 service、回 `SearchResponse` | 不注入 repository、不拋 `HTTPException` |

## 2. Settings 新欄位（`config.py`）

```python
# === OpenSearch（本地 docker，Phase 1 已載入向量）===
opensearch_host: str = "http://localhost:9200"
opensearch_index: str = "products_v1"

# === Bedrock Embedding（與 LLM 段分開：模型/區域皆不同）===
bedrock_embed_model_id: str = "amazon.titan-embed-text-v2:0"
bedrock_embed_region: str = "ap-northeast-1"   # Titan 在東京 lab；LLM 走 us-east-1
embed_dimensions: int = 1024
```

| 欄位 | 預設 | 說明 |
|------|------|------|
| `opensearch_host` | `http://localhost:9200` | 本地 docker，security off 無認證 |
| `opensearch_index` | `products_v1` | Phase 1 建好的 k-NN 索引 |
| `bedrock_embed_model_id` | `amazon.titan-embed-text-v2:0` | **必須與 doc 端嵌入同模型**（§3 不變量） |
| `bedrock_embed_region` | `ap-northeast-1` | Phase 1 已驗證 Titan v2 在東京可用（lab profile） |
| `embed_dimensions` | `1024` | **必須與 doc 端同維度**（§3 不變量） |

mock 路徑沿用既有 `analyzer_mock_mode`（預設 `true`），不新增 flag —— 「本機開發不打真 LLM/embedding」是同一個開關語意。AWS profile 沿用既有 `aws_profile`（lab）。

## 3. 不變量：query 與 doc 必須同模型、同參數、同維度

Phase 1 把 26,014 筆商品以 `amazon.titan-embed-text-v2:0`、`dimensions=1024`、`normalize=true` 嵌入，索引 `embedding` 欄為 faiss/hnsw/**innerproduct**。innerproduct 等價 cosine 的前提是**兩端都是單位向量**。因此：

> **任何 query embedding 必須與 doc 端完全一致：同模型（Titan v2）、同 `dimensions=1024`、同 `normalize=true`。** 任一參數不同 = 兩個向量活在不同空間，k-NN 分數沒有意義 —— 這不是品質下降，是靜默全錯。

落實方式：

- `BedrockEmbeddings` 建構時帶 `model_kwargs={"dimensions": settings.embed_dimensions, "normalize": True}`（Titan v2 request body 參數，與 Phase 1 `embed_query` 的 boto3 body 等價；實作時以一次真實呼叫驗證回傳長度 1024）。
- `normalize=True` **hardcode 在 builder 內**、不做成 Settings —— 它不是可調參數，是空間一致性不變量；做成設定等於留一顆「設錯就靜默全錯」的地雷。
- mock 向量同樣必須是 1024 維**單位向量**（§5），維持 innerproduct 合法。
- 未來換 embedding 模型（Phase 2b benchmark 後）= 全量重嵌 + 新索引（`products_v2` + reindex/alias），不是改一個 Settings 就完事 —— 此事實寫進 spec 的 embedding 契約。

## 4. Embeddings builder（`search/embeddings.py`）

比照 `llm.py` 的 `@lru_cache` 模式（process 層級快取，跨 request 共用、建構同步無 await point 故天然無 race）：

```python
@lru_cache(maxsize=4)
def get_bedrock_embeddings(
    model_id: str,
    region: str,
    profile: str | None,
    dimensions: int,
):
    from langchain_aws import BedrockEmbeddings

    return BedrockEmbeddings(
        model_id=model_id,
        region_name=region,
        credentials_profile_name=profile,
        model_kwargs={"dimensions": dimensions, "normalize": True},
    )
```

- 放在 `search/embeddings.py` 而非 `llm.py`：目前唯一消費者是 search；`llm.py` 管 chat models（ChatBedrockConverse），語意不同。若日後 chains 也需要 embeddings，再升格到頂層（一次 import path 改動）。
- service 呼叫走 **`aembed_query`**（LangChain Embeddings 基類的 async 介面，底層以 executor 包同步 boto3 呼叫）—— 不在 event loop 上直接跑同步 `embed_query`（§7 async 契約）。
- lifespan 非 mock 時預熱（`_preheat_embeddings()`，比照 `_preheat_llm` 的 best-effort try/except：失敗只 log warning 不擋啟動）。

## 5. mock 路徑設計

```python
# search/embeddings.py
MOCK_QUERY_VECTOR: list[float] = [1.0] + [0.0] * 1023   # 1024 維單位向量
```

- **判斷點在 service**（對齊 `AgentService` 既有模式：`__init__` 讀 `settings.analyzer_mock_mode`，方法內 branch）：mock 時 `_embed_query()` 直接回 `MOCK_QUERY_VECTOR`，零網路、零憑證需求。
- **設計取捨 —— 固定向量而非 hash-based 偽語意向量**：mock 的目的是「驗證管線可走」（embed → msearch → RRF → DTO），不是驗證語意品質。固定單位向量讓 k-NN 合法執行（innerproduct 對單位向量合法）、回 deterministic 結果（永遠是最靠近該向量的同一批商品），但**語意不具意義** —— 這是明示的限制，不是 bug。hash-based 偽向量會讓人誤以為 mock 結果有語意參考性，反而有害。
- mock 下 **BM25 半邊完全真實**（不需 Bedrock），所以 mock-mode smoke 仍能驗證「RRF 真的融合了兩個非空清單」—— BM25 邊的結果是真的，k-NN 邊是 deterministic 噪音。
- 真準確度評估（golden set + judge）必須 mock OFF + 真 Titan + 真 OpenSearch，屬 opt-in（§8.3）。

## 6. Repository（`search/repository.py`）

### 6.1 純函式 DSL builder（自 verify_search_os.py lift）

```python
def build_knn_body(vector: list[float], k: int) -> dict:
    return {"size": k, "query": {"knn": {"embedding": {"vector": vector, "k": k}}}}

def build_bm25_body(query_text: str, k: int) -> dict:
    return {
        "size": k,
        "query": {
            "multi_match": {
                "query": query_text,
                "fields": ["martName", "feature", "keyword"],   # 同 Phase 1，smartcn 斷詞
            }
        },
    }
```

純函式、零 I/O —— 單元測試直接斷言 dict 結構，不需 OpenSearch。

### 6.2 hybrid msearch（I/O，與 builder 分離）

```python
class SearchRepository:
    def __init__(self, os_client: AsyncOpenSearch, index: str) -> None: ...

    async def hybrid_msearch(
        self, vector: list[float], query_text: str, k: int
    ) -> tuple[list[dict], list[dict]]:
        """一次 msearch 併發 k-NN 與 BM25，回 (knn_hits, bm25_hits) 兩組 raw hits。"""
        body = [
            {"index": self._index}, build_knn_body(vector, k),
            {"index": self._index}, build_bm25_body(query_text, k),
        ]
        resp = await self._client.msearch(body=body)
        # resp["responses"] 依序對應兩個查詢；任一邊含 error → raise（fail fast，500 走全域 handler）
```

- **`msearch` 的併發語意**：multi-search 是 OpenSearch 端的一次 round-trip、server 端並行執行多查詢 —— 不需要 app 端 `asyncio.gather` 兩條連線。body 為 NDJSON 語意的「header dict + query dict」交錯清單，opensearch-py 接受 list[dict] 自動序列化。
- **部分失敗 fail fast**：msearch 的 per-response error（如某查詢 DSL 錯誤）→ 直接 raise 讓全域 handler 回 500。單邊降級（k-NN 掛了退回純 BM25）是韌性設計，屬 Phase 2b，本次不做（Simplicity First）。
- repository **不讀 `settings`**：`os_client` 與 `index` 由 deps.py 注入（對齊 architecture-convergence 修掉的「repo 讀全域 settings」反模式）。

## 7. RRF 純函式（`search/rrf.py`）

```python
def reciprocal_rank_fusion(
    result_lists: Sequence[Sequence[str]],   # 每個元素是「已排序的 doc id 清單」
    k: int = 60,
) -> list[tuple[str, float]]:
    """score(doc) = Σ_lists 1/(k + rank)，rank 從 1 起算。

    回傳依 score 降序的 (doc_id, score)；同分以 doc_id 字典序 tie-break（deterministic）。
    空清單 / 單邊缺漏皆合法：缺席的清單就是不貢獻分數。
    """
```

介面設計定論：

- **吃 `Sequence[Sequence[str]]`（doc id 清單）而非 raw hits** —— 純資料進出，零 OpenSearch 型別耦合，單元測試餵 `[["a","b"],["b","c"]]` 就能驗證融合正確性。hit metadata（martName、price…）由 service 以 `_id → _source` dict 保留，融合後再 join。
- **可變長清單**而非固定兩個參數 —— Phase 2b 若加第三路訊號（如 category 降權後的重排清單）介面不用改。
- `k=60` 為 RRF 原始論文與業界慣例預設值，曝露為參數但本次不開放到 API query string（沒有調參需求前不曝露）。
- **deterministic tie-break**：同分按 doc_id 排序 —— 測試可重現、線上結果穩定。

## 8. Service 與 Router

### 8.1 SearchService（`search/service.py`）

```python
class SearchService:
    def __init__(self, repo: SearchRepository) -> None:
        self._repo = repo
        self.mock_mode = settings.analyzer_mock_mode   # service 層讀 config 合法

    async def search(self, query: str, size: int = 10) -> SearchResponse:
        vector = await self._embed_query(query)            # mock → MOCK_QUERY_VECTOR
        candidate_k = 2 * size                              # 每邊候選窗
        knn_hits, bm25_hits = await self._repo.hybrid_msearch(vector, query, candidate_k)
        fused = reciprocal_rank_fusion([ids(knn_hits), ids(bm25_hits)])   # k=60
        # 取 top-size → 以 _id→_source map join 回 metadata → SearchResultItem(score=RRF score)
        return SearchResponse(query=query, results=items)
```

- **候選窗 `candidate_k = 2 * size`**（預設 size=10 → 每邊取 20）：RRF 融合需要比最終 size 更寬的單邊視窗，否則「在 A 清單第 11 名 + B 清單第 11 名」這種雙邊中段的好結果會在融合前就被切掉。2× 是夠用的最小倍率；26k 文件的索引取 200（size 上限 100 時）毫無壓力。不做成 Settings —— 沒有調參需求。
- **查無結果回空 `results: []`、HTTP 200** —— 「搜尋沒中」是正常業務結果不是錯誤，與 `GET /recommendations/{id}` 查無特定資源回 404 的語意不同。
- `SearchResultItem.score` 放 **RRF 融合分**（不是 OpenSearch _score —— 兩路 _score 量綱不同本來就不可比，這正是用 RRF 的原因之一）。
- service 回 Pydantic DTO，**不回 raw hit dict**（對齊「Service 不回 ORM/raw 結構」紀律）。

### 8.2 Router（`search/router.py`）

```python
router = APIRouter(prefix="/search", tags=["search"])

@router.get("", response_model=SearchResponse)
async def search(
    service: SearchServiceDep,
    q: str = Query(min_length=1),
    size: int = Query(default=10, ge=1, le=100),   # 對齊 recommendations 的上限慣例
):
    return await service.search(q, size=size)
```

不 try/except、不拋 `HTTPException` —— 未預期錯誤飄給全域 handler（對齊 architecture-convergence 收斂後的 API 層紀律）。

## 9. app 整合：client 生命週期 / deps / lifespan

### 9.1 AsyncOpenSearch client 生命週期（`search/client.py`）

```python
@lru_cache(maxsize=1)
def get_opensearch_client() -> AsyncOpenSearch:
    from opensearchpy import AsyncOpenSearch
    return AsyncOpenSearch(hosts=[settings.opensearch_host])   # 本地 security off，無認證/TLS

async def close_opensearch_client() -> None:
    """lifespan shutdown 呼叫：關閉 aiohttp session 後清快取。"""
    # cache 有值才 close；close 後 cache_clear()，避免殘留已關閉的 client
```

- **比照 `llm.py` 的 lru_cache 模式而非 `app.state`**：(a) 與既有 codebase 一致（一種 singleton 模式就好）；(b) 建構同步、無 await point，天然無 race；(c) deps.py provider 直接呼叫函式，不用從 request 摸 `app.state`。代價是 shutdown 要顯式 close + cache_clear —— 用一支 `close_opensearch_client()` 收斂。
- AsyncOpenSearch 建構**不發網路連線**（lazy connect），startup 建好只是把物件備妥；真正連線在第一次查詢。
- **必須 `await client.close()`**：AsyncOpenSearch 底層是 aiohttp session，不關閉會在 shutdown 噴 unclosed session warning（close 是 coroutine）。

### 9.2 lifespan（`main.py`）

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    _preheat_llm()
    get_opensearch_client()        # 建好 client 物件（lazy connect，不阻塞啟動）
    _preheat_embeddings()          # 非 mock 時 best-effort 建 BedrockEmbeddings（比照 _preheat_llm）
    yield
    await close_opensearch_client()   # Shutdown：關 aiohttp session
```

既有「POC 階段 shutdown 不用做事」的註解隨之更新 —— 這是 app 第一個需要 shutdown 清理的資源。

### 9.3 deps.py（唯一 wiring 點）

```python
def get_os_client() -> AsyncOpenSearch:
    return get_opensearch_client()                    # 共用 process 單例

OSClientDep = Annotated[AsyncOpenSearch, Depends(get_os_client)]

def get_search_repository(os_client: OSClientDep) -> SearchRepository:
    return SearchRepository(os_client, index=settings.opensearch_index)

SearchRepoDep = Annotated[SearchRepository, Depends(get_search_repository)]

def get_search_service(repo: SearchRepoDep) -> SearchService:
    return SearchService(repo)

SearchServiceDep = Annotated[SearchService, Depends(get_search_service)]
```

**取捨 —— wiring 集中 deps.py vs co-locate 進 search/**：領域模組「self-contained」的純粹主義會把 providers 放 `search/deps.py`。定論：**集中 `deps.py`** —— coding-rules 與 architecture-convergence 才剛確立「deps.py 唯一 wiring 點」，第一個新模組就破例會讓規則名存實亡；providers 只有三個函式，集中成本趨近零，而「全 app 的 DI 一眼看完」的價值是實打實的。search 模組保有「業務邏輯 self-contained」，讓渡「wiring self-contained」。

## 10. 測試策略

### 10.1 單元（CI、無 docker、無網路）— `tests/test_search_units.py`

| 對象 | 斷言 |
|------|------|
| `reciprocal_rank_fusion` | 雙清單融合正確性（手算 1/(60+rank) 驗 score 與排序）；交集 doc 分數疊加排前；`k` 參數影響；空清單；單邊缺漏；同分 tie-break deterministic |
| `build_knn_body` / `build_bm25_body` | dict 結構、`size`、欄位清單（martName/feature/keyword）、向量原樣帶入 |
| `MOCK_QUERY_VECTOR` | 長度 1024、L2 norm == 1.0（單位向量不變量） |
| `SearchService` 編排 | 注入 fake repo（回預製 hits 兩組）+ mock mode：驗證融合→top-size→DTO 映射、空結果回空 list |

### 10.2 mock-mode API smoke（需 OpenSearch，不需 Bedrock）— `tests/test_search_api_smoke.py`

- 前置：`docker compose -f docker-compose.dev.yml --env-file .env.local up -d opensearch`（Phase 1 volume 已含 26,014 筆 + 向量）。OpenSearch `localhost:9200` 不可達 → 整模組 `pytest.mark.skipif` skip（對齊 `test_pipeline_e2e.py` 的 DB reachability 模式）。
- conftest 已強制 `ANALYZER_MOCK_MODE=true` → 零 Bedrock。
- 斷言：`GET /search?q=掃地機器人` 回 200、`results` 非空、每筆含 `mart_id`/`mart_name`/`score`、score 降序；**融合證據**：結果含 BM25 詞面命中（mock 向量下 k-NN 邊是噪音、BM25 邊是真的，詞面強 query 必有 BM25 貢獻）；`size` 邊界（`size=101` 回 422、`q=` 空回 422）；空結果 query 回 200 + `[]`。

### 10.3 準確度評估（opt-in，真 Bedrock + OpenSearch）— `scripts/etl/judge_hybrid_search.py`

- 重用 Phase 1 量尺：golden set（15 條 approved，approved gate 照舊程式化強制）+ `judge_search_relevance.py` 的 LLM-judge 方法（Opus 級 judge，Phase 1 結論已建議）。
- 對**運行中的 app**（mock OFF）打 `GET /search`，取 hybrid top-10；**同一輪**並排重跑 k-NN-only 與 BM25-only（同 judge 同批評，避免跨輪 judge 漂移），三欄比較。
- 成功標準：**hybrid 不劣於單一方法** —— 全局 hybrid 相關數 ≥ max(vec 相關數, bm25 相關數)，且向量強項 query（q11 類情境式）與 BM25 強項 query（q04 ThinkPad）hybrid 都不可歸零（互補性必須保留，不是平均掉）。未達照 Phase 1 慣例**如實回報，不調寬判定**。
- **花錢 gate**：15 query 嵌入 + 三路 × ~10 商品 × judge ≈ 數百次 Haiku/Opus 呼叫，量級 < $1，但仍是真 Bedrock —— 執行前必須告知使用者並取得同意（safety.md §1）；非 CI、非預設。

## 11. 關鍵設計取捨

| 取捨 | 定論 | 理由 |
|------|------|------|
| ⭐ 融合：應用端 Python RRF vs OpenSearch native search pipeline（score-ranker-processor，2.19 已有） | **應用端 Python RRF** | (a) 純函式可單測：`Σ 1/(k+rank)` 十行內，pytest 全覆蓋；pipeline 的融合行為只能整合測試。(b) 重用 POC：knn/bm25 查詢 DSL 從 verify 腳本直接 lift，pipeline 要改寫成 hybrid query + pipeline 設定檔。(c) 不新增 OpenSearch 端狀態（pipeline 是 cluster 資源，要建立/版控/遷移）。(d) 26k 文件、每邊 ≤200 候選，app 端融合成本微秒級。代價：放棄 server 端單查詢 round-trip 優化 —— 用 msearch 一次 round-trip 補回 |
| ⭐ 領域模組 `search/` vs 散進既有 `repositories/` `services/` `api/` | **領域模組**（計劃 §Phase 2 已拍板） | search 是第一個 infra 斷層 domain（OpenSearch ≠ Postgres+Bedrock 核心鏈）；圈起來才看得見邊界、可獨立抽換。模組內部仍守三層紀律 + deps.py 集中 wiring（§1.1、§9.3），不是治外法權 |
| ⭐ AsyncOpenSearch vs 同步 OpenSearch client | **AsyncOpenSearch**（`opensearch-py[async]`，aiohttp） | app 全 async（FastAPI + asyncpg + aioboto3）；同步 client 會阻塞 event loop，一個慢查詢卡死全部 in-flight request。官方 async guide 即此安裝方式 |
| ⭐ client 生命週期：lru_cache 模組單例 vs `app.state` | **lru_cache + lifespan 顯式 close**（§9.1） | 與 `llm.py` 既有模式一致；deps provider 不必摸 request；代價（shutdown 顯式 close + cache_clear）收斂在一支函式 |
| ⭐ mock 向量：固定單位向量 vs hash-based 偽語意向量 | **固定 `[1.0, 0…]`** | mock 驗管線不驗語意；固定向量 deterministic、保 innerproduct 合法；偽語意向量是過度設計且誤導（§5） |
| ⭐ RRF 介面：吃 doc id 清單 vs 吃 raw hits | **`Sequence[Sequence[str]]` → `list[(id, score)]`** | 零 OpenSearch 耦合、測試餵字串清單即可；metadata join 是 service 的映射職責（§7） |
| 兩查詢併發：`msearch` 一次 round-trip vs `asyncio.gather` 兩條 `search` | **msearch** | 一次網路 round-trip、server 端並行；gather 兩條連線沒有額外好處還多一倍連線開銷 |
| msearch 單邊失敗：fail fast vs 降級回單路 | **fail fast → 500** | 降級是韌性功能，沒人要求；靜默降級還會讓準確度悄悄掉而無人知（寧可 500 被看見）。Phase 2b 再議 |
| embeddings builder 位置：`search/embeddings.py` vs `llm.py` 旁 | **search 模組內** | 唯一消費者是 search；llm.py 語意是 chat model。出現第二個消費者再升格 |
| `normalize` 做成 Settings？ | **不做，hardcode True** | 是空間一致性不變量不是參數（§3）；可設定 = 留靜默全錯地雷 |
| 候選窗 `candidate_k` | **`2 * size`，hardcode** | 給融合視窗的最小夠用倍率；無調參需求不上 Settings（§8.1） |
| RRF `k=60` 曝露到 API？ | **函式參數有、API 無** | 業界慣例預設；query string 曝露調參介面是沒有需求的表面積 |
