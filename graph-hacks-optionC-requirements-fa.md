# Graph Hacks — گزینه C: Multi-Agent Coordination روی گراف
### به چه چیزهایی برای اجرا نیاز داریم؟

> ایده: دو (یا سه) agent که تسک‌ها را روی یک گراف مشترک تقسیم می‌کنند، وابستگی‌ها را می‌فهمند و با هم هماهنگ می‌شوند. جذاب برای داور چون هر سه Track را پوشش می‌دهد: *agents that act on connected data + agent memory/coordination + company brain*.

---

## 1) معماری ساده (همین را بساز)

```
[User / Task Input] 
      ↓
[Coordinator Agent] — گراف را می‌خواند، تسک را می‌شکند، به Workerها می‌دهد
      ↓                ↓
[Worker A] ←——→ [FalkorDB Graph] ←——→ [Worker B]
 (مثلاً Research)         ↑              (مثلاً Execution)
                     [Memory / History]
                          ↓
                   [ویژوال گراف + لاگ هماهنگی]
```

**سناریوی دموی پیشنهادی (ساده و قابل فهم برای داور):**
- ورودی: «یک کمپین مارکتینگ برای محصول X بساز»
- Coordinator گراف را می‌بیند (تیم، مهارت‌ها، تسک‌های قبلی، وابستگی‌ها)
- تسک را می‌شکند: Research → Content → Design → Publish
- Worker A و B به‌صورت موازی روی نودهای گراف کار می‌کنند، status را روی گراف آپدیت می‌کنند
- در نهایت گرافِ «چه کسی چه کاری را چه زمانی انجام داد» نمایش داده می‌شود

---

## 2) چک‌لیست نیازمندی‌ها

### A. حساب‌ها (همه رایگان، بدون تحریم)

| چیز | چرا لازم | هزینه | لینک/دستور |
|---|---|---|---|
| **FalkorDB** | دیتابیس گراف اصلی (شرط اجباری مسابقه) | رایگان | **گزینه 1 - Cloud:** https://app.falkordb.cloud (سریع‌تر) <br> **گزینه 2 - Docker:** `docker run -p 6379:6379 -p 3000:3000 falkordb/falkordb:latest` |
| **LLM API** | مغز agentها | رایگان/ارزان | OpenAI (`gpt-4o-mini`) یا Anthropic یا حتی مدل لوکال (Ollama). برای شروع یک API key کافی است |
| **GitHub** | سابمیشن + کد | رایگان | ریپوی public |
| **WeMakeDevs** | ثبت‌نام مسابقه | رایگان | https://www.wemakedevs.org/hackathons/falkordb |

> نکته ایران: FalkorDB Cloud و Docker هر دو بدون نیاز به احراز هویت تحریمی کار می‌کنند. اگر Cloud با IP ایران اذیت کرد، Docker لوکال 100% جواب است.

### B. نرم‌افزار روی سیستم

- **Python 3.10+** یا **Node.js 18+** (یکی کافی است — Python پیشنهاد می‌شود)
- **Docker** (اگر می‌خواهی FalkorDB لوکال بیاوری)
- **Git**
- پکیج‌ها:
  ```bash
  pip install falkordb openai python-dotenv networkx
  # یا برای ویژوال:
  pip install flask  # برای یک UI ساده
  ```
  درایور رسمی: `falkordb` (پایتون) — داک: https://docs.falkordb.com

### C. دانش فنی لازم (حداقلی)

- بلد باشی یک کوئری Cypher ساده بنویسی (مثل SQL برای گراف):
  ```cypher
  CREATE (a:Agent {name: 'WorkerA', skill: 'research'})
  CREATE (t:Task {id: 't1', title: 'Research market', status: 'pending'})
  CREATE (a)-[:ASSIGNED_TO]->(t)
  MATCH (a:Agent)-[:ASSIGNED_TO]->(t:Task) RETURN a, t
  ```
- بلد باشی یک agent ساده با LLM بسازی (system prompt + tool calling)
- نیازی به دانش قبلی گراف نیست — داورها همین را گفته‌اند

### D. سخت‌افزار

- لپ‌تاپ معمولی کافی است. FalkorDB بسیار سبک است (زیر 500MB RAM).
- حتی روی سرور رایگان (Codespaces / Replit) هم اجرا می‌شود.

---

## 3) اسکیمای گراف پیشنهادی (همین را پیاده کن)

```cypher
// نودها
(:Agent {id, name, role, status})      // مثلاً Coordinator, WorkerA, WorkerB
(:Task {id, title, status, priority})  // pending | in_progress | done
(:Skill {name})                         // research, writing, design
(:Person {name})                        // اگر خواستی company brain اضافه کنی

// یال‌ها
(:Agent)-[:HAS_SKILL]->(:Skill)
(:Agent)-[:ASSIGNED_TO]->(:Task)
(:Task)-[:DEPENDS_ON]->(:Task)         // وابستگی تسک‌ها
(:Task)-[:COMPLETED_BY]->(:Agent)
(:Agent)-[:COORDINATES]->(:Agent)
```

همین 5 نود و 5 یال برای دمو کافی است — پیچیده نکن.

---

## 4) استک پیشنهادی (سریع‌ترین مسیر تا دمو)

**گزینه پیشنهادی (Python):**

| لایه | ابزار |
|---|---|
| گراف | FalkorDB (Cypher) |
| Agentها | Python + OpenAI API (یا هر LLM) — هر Worker یک system prompt جدا |
| هماهنگی | Coordinator یک حلقه ساده: گراف را می‌خواند → تصمیم می‌گیرد → Worker را صدا می‌زند |
| بک‌اند | یک فایل `app.py` با Flask/FastAPI (اختیاری) |
| فرانت | یک صفحه HTML ساده + `vis-network` یا `D3.js` برای نمایش گراف + لاگ چت agentها |
| دیپلوی | لوکال کافی است؛ برای سابمیشن یک ویدیو 2 دقیقه‌ای + لینک GitHub |

**اسکلت کد (شبه‌کد واقعی):**

```python
from falkordb import FalkorDB
import openai

db = FalkorDB(host='localhost', port=6379)
graph = db.select_graph('coordination')

# 1. ساخت گراف اولیه
graph.query("CREATE (:Agent {name: 'Coordinator', role: 'planner'})")
graph.query("CREATE (:Agent {name: 'WorkerA', role: 'researcher'})")
graph.query("CREATE (:Agent {name: 'WorkerB', role: 'executor'})")
graph.query("CREATE (t1:Task {id:'1', title:'Research', status:'pending'})")
graph.query("CREATE (t2:Task {id:'2', title:'Write content', status:'pending'})")
graph.query("MATCH (a:Task {id:'1'}), (b:Task {id:'2'}) CREATE (b)-[:DEPENDS_ON]->(a)")

# 2. Coordinator: گراف را می‌خواند، تصمیم می‌گیرد
def coordinator_decide():
    result = graph.query("MATCH (t:Task {status:'pending'}) WHERE NOT (t)-[:DEPENDS_ON]->(:Task {status:'pending'}) RETURN t")
    # ... به LLM بده تا تصمیم بگیرد کدام Worker برای کدام Task
    pass

# 3. Worker: تسک را اجرا می‌کند و گراف را آپدیت می‌کند
def worker_execute(task, worker_name):
    # LLM call ...
    graph.query(f"MATCH (t:Task {{id:'{task}'}}) SET t.status='done'")
    graph.query(f"MATCH (a:Agent {{name:'{worker_name}'}}), (t:Task {{id:'{task}'}}) CREATE (a)-[:COMPLETED_BY]->(t)")
```

---

## 5) چه چیزی باید تحویل بدهی (طبق قوانین Graph Hacks)

1. **ریپوی GitHub public** با README (توضیح ایده + اسکرین‌شات گراف)
2. **ویدیوی دمو 1–2 دقیقه** (اسکرین‌رکورد کافی است)
3. **استفاده واقعی از FalkorDB** — نه تزئینی (داور چک می‌کند کوئری‌ها واقعی باشند)
4. **سابمیشن در WeMakeDevs** قبل از 18 Oct 11:59 PM IST

---

## 6) تایم‌لاین 36 ساعته (واقع‌بینانه)

| بازه | کار |
|---|---|
| **امروز 14:30–16:00** | ثبت‌نام + FalkorDB بالا آوردن + اسکیمای گراف |
| **16:00–21:00** | Coordinator + یک Worker + اتصال LLM (MVP) |
| **21:00–23:00** | Worker دوم + منطق DEPENDS_ON + آپدیت گراف |
| **فردا 09:00–12:00** | ویژوال گراف (vis-network) + لاگ هماهنگی |
| **12:00–14:00** | ویدیو + README + سابمیشن |

**کل زمان لازم:** حدود 10–14 ساعت مفید — با یک نفر هم شدنی است.

---

## 7) ریسک‌ها و نکات

- **پیچیده نکن:** داور گراف واقعی می‌خواهد، نه 10 agent. دو Worker + یک Coordinator برنده‌تر از 5 Worker نصفه‌نیمه است.
- **آفلاین هم می‌شود:** اگر API LLM قطع شد، با Ollama لوکال (`llama3.1:8b`) ادامه بده.
- **جایزه سخت‌افزاری:** ارسال iPhone/PS5 به ایران ممکن نیست ولی پروژه برای رزومه و اعتبار بین‌المللی عالی است — تیشرت Top 50 هم می‌رسد.

---

### جمع‌بندی: برای شروع همین الان فقط این 3 دستور کافی است

```bash
docker run -d -p 6379:6379 -p 3000:3000 falkordb/falkordb:latest
pip install falkordb openai python-dotenv
# بعد یک فایل app.py با اسکلت بالا بساز و گراف را تست کن
```

می‌خواهی اسکلت کامل `app.py` + صفحه ویژوال را هم همین الان بسازم؟
