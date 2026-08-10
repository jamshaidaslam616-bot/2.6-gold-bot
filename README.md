# Gold 2.6 Strategy Bot

XAUUSD ke liye "2.6 retracement" strategy — teen timeframes (M5 / M15 / H1),
12-mahine ka backtester, aur MT5 demo par live daemon with Telegram alerts.

> **Demo only.** Live trading ke liye do alag unlocks chahiye jo sirf account owner
> set kar sakta hai. Code mein koi raasta nahi jo inhein bypass kare.

---

## 1. Strategy — 2.6 kya hai

**Step 1 — Break of Structure.** Price pichla swing high/low torta hai, aur break tab
hi ginta hai jab candle ki **body close** us level ke paar ho. Sirf wick nikalna break
**nahi** hai — yehi ek rule is strategy ko us bot se alag karta hai jo har liquidity
sweep par fire ho jaye.

**Step 2 — impulse wave, wick-to-wick.** BOS ke baad move tab tak chalti hai jab tak
naya swing confirm na ho:

```
Origin = jahan se move shuru hui   (bullish: lowest low WICK)
Peak   = jahan move khatam hui     (bullish: highest high WICK)
```

**Step 3 — 2.6 level.**

```
Range  = |Peak - Origin|
Result = Range / 2.6

bullish:  entry = Peak - Result        (high se minus)
bearish:  entry = Peak + Result        (low mein plus)
```

Dono lines ek hi baat kehti hain: **Peak se 38.46% wapas retrace**. Yeh `Origin + Result`
se **alag price** hai (woh 61.5% level hai) — dono ko confuse karna har trade ulti kar
deta hai.

**Step 4 — brackets.** Stop bilkul **Origin** par, target kam az kam 1:2.

**Step 5 — invalidation.** Agar price 2.6 level ko chhue baghair Origin tor de, setup
mar gaya. Naye BOS ka intezaar.

### Geometry ka natija — jaanna zaroori hai

```
risk = Range - Result   = 0.6154 · Range
tp   = entry + 2·risk   = Peak + 0.846 · Range      (bullish)
```

Target **Peak se 84.6% Range upar** hota hai. Har jeetne wali trade ko bara naya extreme
banana parta hai. Isi liye win rate **construction ke hisab se** 50% se kaafi neeche
rahega — yeh kharabi nahi, design hai. Lekin iska matlab yeh bhi hai ke average win bara
hona chahiye, aur costs theek wohi cheez khate hain.

---

## 2. File tree

```
gold-2.6-bot/
├── config.py          risk limits, strategy tunables, .env loader, live locks
├── logger.py          rotating file + console logging
├── data.py            MT5 connection, symbol resolution, chunked history + parquet cache
├── engine.py          ★ swing detection + 2.6 levels. Backtest aur live DONO yehi use karte hain
├── risk.py            sizing, daily loss, max drawdown, kill switch
├── journal.py         har trade ka CSV record
├── execution.py       order routing, forced brackets, idempotency
├── telegram_bot.py    async alerts (httpx)
├── backtester.py      12-month replay + report + CSV export
├── main.py            live async daemon
└── tests/             48 tests — look-ahead proof, sizing, halts, naked-order refusal
```

`engine.py` par star isliye hai ke backtester **alag runner** hai, **alag implementation
nahi**. Agar simulator apne rules likhta to woh strategy naapta jo kabhi ship hi nahi hoti.

---

## 3. Install

```powershell
cd C:\Users\Administrator\gold-2.6-bot
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

MT5 terminal machine par install aur chalta hua hona chahiye — Python API terminal se
baat karta hai, broker se seedha nahi.

---

## 4. Credentials

```powershell
copy .env.example .env
notepad .env
```

`.env` gitignored hai, kabhi log ya print nahi hota (`Secrets.__repr__` redacted hai).

**Telegram chat ID** nikalne ke liye: apne bot ko koi bhi message bhejein, phir

```powershell
.venv\Scripts\python.exe telegram_bot.py
```

Yeh chat ID print kar dega. Telegram set na ho to bot **chalega**, bas alerts skip karega —
alerts band hone se open risk manage karna nahi rukna chahiye.

---

## 5. Backtest chalayein

```powershell
.venv\Scripts\python.exe backtester.py                      # 12 mahine, poore costs, halts on
.venv\Scripts\python.exe backtester.py --no-costs           # raw edge — costs ka asar dekhne ke liye
.venv\Scripts\python.exe backtester.py --no-halts           # halts band, poora saal
.venv\Scripts\python.exe backtester.py --months 6 --refresh # cache ignore karke naya data
```

Report screen par aati hai aur har trade `reports/backtest.csv` mein jaati hai.

---

## 6. Live daemon

```powershell
.venv\Scripts\python.exe main.py
```

`Ctrl+C` se rukta hai aur nikalte waqt apne resting orders cancel kar deta hai.

Kill switch fire ho jaye to `runtime/KILL_SWITCH` file ban jaati hai aur bot naya position
nahi kholega. **Yeh file khud nahi mitti** — pehle samjhein kyun fire hui, phir delete karein.

---

## 7. Backtest results — 12 mahine, XAUUSDm

Spec-correct engine (BOS + `Peak − Result` entry + stop Origin par). Mini account ki
sahi economics: **commission $0**, cost poora spread mein (median 160–192 points),
20 points slippage, owner ki risk limits enforced.

```powershell
.venv\Scripts\python.exe backtester.py --commission 0
```

| Timeframe | Trades | Win Rate | Break-even chahiye | PF | Max DD | Net Return | |
|---|---|---|---|---|---|---|---|
| M5  | 245 | 34.7% | 33.9% | 1.03 | 10.20% | **+2.62%** | halted 2025-12-18 |
| M15 | 263 | 37.6% | 34.3% | 1.16 | 8.69%  | **+10.44%** | |
| H1  | 66  | 34.8% | 31.7% | 1.15 | 3.37%  | **+2.50%** | |

Teeno positive dikhte hain. **Yeh natija qabil-e-aitmaad nahi — agla section parhein.**

## 7b. 4 saal ka test — asal jawab

Broker ke paas 4 saal ka data hai, sirf 1 nahi. 12 mahine ka natija ek saal ka namoona
tha; 4 saal ka namoona alag kahani sunata hai.

```powershell
.venv\Scripts\python.exe backtester.py --months 48 --commission 0 --no-halts
```

Yeh numbers **wave-measurement bug theek karne ke baad** ke hain (section 8 dekhein).

| TF | Trades | Win Rate | PF | Max DD | Net | mean R | t-stat | 95% CI on mean R |
|---|---|---|---|---|---|---|---|---|
| M5  | 1038 | 33.5% | 0.99 | 22.21% | **−2.05%** | +0.002 | 0.05 | [−0.084, +0.089] |
| M15 | 1000 | 34.1% | 1.01 | 18.66% | **+4.46%** | +0.017 | 0.39 | [−0.071, +0.106] |
| H1  | 246  | 30.1% | 0.86 | 12.41% | **−9.69%** | −0.100 | −1.14 | [−0.272, +0.072] |

**Teeno ka 95% confidence interval zero ko cross karta hai.** Kisi bhi timeframe ka edge
zero se alag sabit nahi hota. Yeh sabooot nahi ke strategy loss deti hai — yeh sabooot ki
*ghair-maujoodgi* hai ke woh jeet ti hai.

Halts enforced karke (asal system) teeno **10% drawdown ceiling tak pohanche**:
M5 −0.24% (halted 2025-06-19), M15 +3.17% (halted 2024-09-05), H1 −6.81% (halted 2025-04-03).

Saal-ba-saal (net USD, no-halts):

| TF | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|
| M15 | −188 | +691 | −538 | +70 | +410 |
| H1 | −479 | +286 | −475 | −217 | −84 |

M15 ka mean R 12 mahine ke namoone mein **+0.130** tha; 4 saal par **+0.017**. Chhota
namoona khush-qismati thi, edge nahi.

## 7c. Take-profit search — Zero account, 4 saal, out-of-sample ke sath

Owner ne kaha "TP aisa rakho ke profitable rahe". Yeh maangna jaiz hai lekin seedha karna
khatarnak — 10 variants ek hi tareekh par test karo to koi na koi ittefaqan achha nikal
hi aayega. Isliye `optimise.py` har candidate ko **do alag arse** par naapta hai:

```powershell
.venv\Scripts\python.exe optimise.py --timeframes M15 --commission 5.50 --symbol XAUUSD
```

Account `472250693` (Zero), `XAUUSD`, 93,123 M15 bars, commission $5.50/side.
IN-SAMPLE 2022-08 → 2025-01, OUT-OF-SAMPLE 2025-01 → 2026-08.

| take profit | IS n | IS mean R | IS PF | OOS n | OOS mean R | OOS PF | OOS win% |
|---|---|---|---|---|---|---|---|
| peak (0.6R) | 1087 | −0.059 | 0.86 | 641 | −0.015 | 0.95 | 61.3% |
| peak +25% | 862 | −0.013 | 0.97 | 549 | +0.033 | 1.07 | 51.4% |
| peak +50% | 708 | −0.002 | 1.00 | 485 | +0.065 | 1.11 | 44.1% |
| rr 0.75 | 1018 | −0.037 | 0.91 | 635 | +0.012 | 1.02 | 58.4% |
| rr 1.00 | 881 | −0.010 | 0.98 | 553 | +0.024 | 1.04 | 51.7% |
| rr 1.25 | 772 | −0.017 | 0.97 | 508 | +0.039 | 1.08 | 46.7% |
| rr 1.50 | 698 | **+0.004** | 1.00 | 472 | **+0.054** | 1.08 | 42.6% |
| **rr 2.00 (SPEC)** | 597 | **+0.007** | 1.00 | 407 | **+0.029** | 1.05 | 34.6% |
| rr 2.50 | 517 | +0.035 | 1.05 | 356 | −0.037 | 0.96 | 27.8% |
| rr 3.00 | 474 | +0.061 | 1.08 | 329 | −0.001 | 0.99 | 25.2% |

**Best in-sample tha `rr 3.00` (+0.061 R). Out-of-sample woh `−0.001` par gir gaya.**
Yeh curve-fitting ki tareef hai, kuch aur nahi.

Sirf **2/10** candidates dono arson mein positive rahe: `rr 1.50` aur **`rr 2.00` — yaani
owner ka apna spec**. Koi bhi kahin `t > 2` tak nahi pohancha.

Ghaur karne wali baat: OOS column mein taqreeban sab kuch positive hai aur IS column mein
taqreeban sab kuch negative. **TP settings ka farq arson ke farq se chhota hai** — matlab
TP woh lever hai hi nahi.

Zero account ne bhi madad nahi ki: spec ka mean R mini par `+0.017`, Zero par IS `+0.007` /
OOS `+0.029`. Wohi baat.

## 7d. Saat timeframes — kis mein edge hai?

Owner ne kaha timeframe par koi bandish nahi, jis mein edge ho woh lo. Spec config
(`rr 2.00`), Zero account, 4 saal, wohi IS/OOS split:

```powershell
.venv\Scripts\python.exe optimise.py --spec-only --timeframes M1,M5,M15,M30,H1,H4,D1 --symbol XAUUSD
```

| TF | IS n | IS mean R | IS t | OOS n | OOS mean R | OOS t | dono mein +? |
|---|---|---|---|---|---|---|---|
| M1 | 573 | −0.110 | −1.90 | 376 | −0.007 | −0.09 | ✗ |
| M5 | 612 | −0.009 | −0.16 | 403 | +0.027 | +0.38 | ✗ |
| **M15** | 597 | **+0.007** | +0.12 | 407 | **+0.029** | +0.41 | **✓** |
| M30 | 349 | −0.091 | −1.23 | 212 | +0.011 | +0.11 | ✗ |
| H1 | 161 | −0.118 | −1.09 | 88 | −0.086 | −0.58 | ✗ |
| H4 | 35 | −0.149 | −0.64 | 12 | −0.254 | −0.65 | ✗ |
| D1 | 2 | — | — | 0 | — | — | namoona hi nahi |

**Sirf M15 dono arson mein positive hai.** Lekin kahin bhi `|t|` 2 tak nahi pohancha — sab
se bara M1 ka `−1.90` hai, aur woh manfi taraf.

### Do warnings jo is table ke sath zaroori hain

**1. M1 aur M5 ka arsa alag hai.** MT5 har timeframe ke ~100,000 bars hi rakhta hai, isliye
M1 ka data sirf Apr–Aug 2026 ka hai aur M5 ka Mar 2025 se. M15/M30/H1 poore 4 saal ke hain.
Yeh apples-to-apples nahi.

**2. Saat timeframes test karne ka matlab saat mauqe hain.** Agar saaton ka asal edge zero
ho, tab bhi kam az kam ek ka dono arson mein positive nikalna **87% imkaan** rakhta hai
(`1 − 0.75⁷`). M15 ka survive karna theek wohi cheez hai jo mehez ittefaq se tawaqqo ki
jati hai. Woh sabooot nahi.

### Isay sabit karne ke liye kitne trades chahiye?

`n = (1.96 × 1.43 / 0.017)² ≈ 27,000 trades` — lagbhag **100 saal** ka M15 data.

Yeh sab se ahem jumla hai is README mein: **is size ka edge, agar hai bhi, na kabhi sabit
ho sakta hai na kamaya ja sakta hai** — is instrument aur in costs par.

### Commission account-type par depend karta hai — inhe mix mat karein

Yeh maine dono accounts par **asal deals se verify kiya**:

| Account | Symbol | Spread | Commission |
|---|---|---|---|
| Zero (472250693) | `XAUUSD` | median **0** points | **$5.50/side** ($0.11 per 0.01 lot round-turn) |
| Mini (472286354) | `XAUUSDm` | median **160–192** points | **$0.00** (5.0 lot deals par bhi zero) |

Zero ka commission mini ke data par lagana costs **do baar** count karta hai. Isi liye
`--commission` flag hai. Naya demo account banate waqt uska type batayein.

### Costs ka asar

Wohi strategy, saare costs zero:

| Timeframe | Costs zero | Asal costs |
|---|---|---|
| M5  | PF 1.10 | PF 1.03 |
| M15 | PF 1.20 | PF 1.16 |
| H1  | PF 0.96 | PF 1.15 |

Edge asal mein patla hai (PF 1.03–1.20) aur costs ka hissa uska bara portion hai. M5 par
costs net P&L ke **109%** ke barabar hain — yaani jitna profit bacha, us se zyada costs mein
gaya. **Yeh sab se aam tareeqa hai jis se backtest achha dikhta hai aur live account khali
hota hai**, isi liye cost model andaze par nahi, asal fills se naapa gaya hai.

---

## 8. Backtest kahan jhoot bolta hai, aur yahan uska ilaaj

| Khatra | Ilaaj |
|---|---|
| **Sub-leg measurement** (asal bug, 2026-08-07 ko pakda) | Trend mein price baar baar structure torta hai. Purana code Origin ko har naye break se pehle wali *aakhri* pivot par anchor karta tha — yaani chhoti pullback low. 12 mahine M15 par: **49% setups continuation break par bane, 15% ne lahar ka aadha se kam naapa, worst 11.8x chhota** ($10.71 vs $125.91). Ab har break apne directional run ke shuru ka pointer rakhta hai, aur Origin poori lahar ka lowest wick hai. Regression test: `test_continuation_break_does_not_reanchor_the_origin` |
| **Look-ahead** — fractal ko confirm hone ke liye daayein N bars chahiye | Har `Swing` `confirmed_at = index + right` rakhta hai. 5 tests, jin mein *truncation invariance* — poori series se bar `t` par jo nazar aata hai, woh `t` par khatam hone wali series se bilkul wohi hona chahiye. Impulse par bhi alag se lagta hai, kyunke Origin/Peak ab bar-ranges par scan hote hain |
| **Forming bar** | Har read `start_pos=1` se, aur har bar ka closed hona alag se check hota hai |
| **Intrabar sequence** — ek hi bar mein SL aur TP dono | M15/H1 ke liye andar ke M5 bars se **dekha** jata hai, maana nahi. M5 ke liye tie hamesha **hamare khilaf** |
| **Fill trigger** — buy limit **ask** par fill hoti hai, bars **bid** ke hain | Trigger `bid ≤ entry − spread`. Yeh theek karne se M15 trades 849 se 410 reh gaye — bug material tha |
| **Commission** | Account type par depend karta hai, aur dono asal deals se verify kiye: Zero = $5.50/side, mini = $0.00. Ek ka figure doosre ke data par lagana costs do baar ginta hai |
| **Spread filter** | Absolute point count kaam nahi karta: Zero ka median spread **0** hai, mini ka **192**. Filter instrument ki apni distribution ka p95 hai |
| **Risk limits ignore karna** | Daily halt aur drawdown kill switch replay ke dauran **enforce** hote hain. Strategy March mein 10% tord de to simulation March mein rukta hai — kyunke live bot bhi wahi karta |

---

## 9. Original brief mein jo ghaltiyan theek kin

| Brief mein | Haqeeqat |
|---|---|
| **Entry formula ki directions ulti thin** | Brief: bullish = `Low + Range/2.6` (61.5% level). Spec: bullish = `Peak − Result` (38.5% level). Wohi do formulay, ulti directions par. Yeh **har trade ka entry price** badalta hai |
| **BOS ka koi zikr nahi** | Spec ka pehla step hi BOS hai, body-close confirmation ke sath. Brief mein sirf "swing detection" thi |
| **SL "right outside" the extreme** | Spec: stop bilkul **Origin par**, buffer ke baghair |
| `symbol_info_tick("XAUUSD").spread` | **Aisa field hai hi nahi.** Tick fields: ask, bid, last, time, time_msc, volume, volume_real, flags. `AttributeError` pehli trade par. Sahi: `symbol_info().spread` ya `(ask-bid)/point` |
| `copy_rates_from_pos` se 12 mahine 5m | Ek call mein `None (Invalid params)`. 65,000 bars ki limit hai. 30-din chunks mein 70,018 bars aate hain |
| `"XAUUSD"` hardcoded | Ek account par `XAUUSD` hai, doosre par `XAUUSDm`. Runtime par resolve hota hai |
| 1% risk per trade | Owner ki standing limit **0.5%** hai. Woh maine nahi barhai |
| Koi daily loss / max DD / kill switch nahi | `risk.py` add kiya — 3% daily, 10% DD, file-based kill switch |
| Teen timeframes azad, 3 positions | Owner ne **ek position at a time** chuna. Magic numbers ab attribution ke liye hain; do timeframe ek sath signal dein to H1 > M15 > M5 |
| "asynchronous" lekin `requests` | `requests` sync hai. `httpx.AsyncClient` use kiya |
| Credentials `config.py` mein | `.env` mein, gitignored, redacted repr |
| Pending orders ki expiry nahi | TTL broker-side expiry + supersede/invalidate par cancel |

---

## 10. Abhi kya chahiye (yeh main khud nahi kar sakta)

1. **Naya Exness demo account** — login number aur server name. Password aap khud `.env`
   mein daalein, mujhe bhejne ki zaroorat nahi.
2. **Doosra MT5 terminal install** — MT5 ek waqt mein sirf **ek** account par login rehta hai,
   aur jo bhi aakhir mein `initialize(login=...)` call kare wohi jeet ta hai. Is machine par
   yeh clash abhi ho raha hai. `MT5_TERMINAL_PATH` isi liye hai.
3. **Telegram chat ID** — section 4 dekhein, 30 second ka kaam.

In teenon ke baghair **backtester poora chalta hai** (usay sirf price data chahiye, orders nahi).
Sirf `main.py` inke intezaar mein hai.

---

## 11. Tests

```powershell
.venv\Scripts\python.exe -m pytest tests/ -v
```

48 tests. Sab se ahem `test_truncation_invariance` hai — woh sabit karta hai ke engine
mustaqbil nahi padh raha. Look-ahead isliye khatarnaak hai ke woh backtest ko **behtar**
dikhata hai, to output dekh kar pata nahi chalta; usay sabit karna parta hai.
