# VPS Deployment Guide

Naye Windows VPS par is bot ko chalane ka poora tareeqa.

---

## 0. Pehle ek baat saaf — "terminal band ho to bhi chale"

**Yeh mumkin nahi hai, aur main aap ko sach bata raha hoon ta ke aap ghalat
tawaqqo par plan na banayein.**

`MetaTrader5` Python package broker se seedha baat **nahi** karta. Woh aap ke
computer par chalte hue `terminal64.exe` se baat karta hai (IPC ke zariye).
Terminal band = koi raasta nahi. Yeh library ki bunyadi shakl hai, koi setting
nahi jo isay badal de.

Exness (ya koi bhi retail MT5 broker) aam customers ko REST API nahi deta, to
terminal ko bypass karne ka koi tareeqa nahi.

### Lekin jo aap waqai chahte hain, woh **mumkin hai**

Aap ka asal maqsad shayad yeh hai: *"main RDP band kar doon, laptop band kar
doon, aur bot chalta rahe."* **Woh bilkul hota hai** —

| Aap ki fikr | Haqeeqat |
|---|---|
| "Main RDP se nikal jaonga" | VPS ka session chalta rehta hai. Terminal aur bot dono chalte rehte hain. **Sirf "Disconnect" karein, "Sign out" nahi** — sign out session maar deta hai |
| "Mera laptop band ho jayega" | Koi farq nahi. Sab kuch VPS par chal raha hai |
| "Terminal crash ho gaya to?" | Bot khud `initialize(path=...)` se **dobara launch kar deta hai** |
| "Bot crash ho gaya to?" | `run.ps1` use se dobara start kar deta hai (backoff ke sath) |
| "VPS reboot ho gaya to?" | Auto-logon + scheduled task — section 5 |
| "Sab kuch mar gaya aur position khuli thi?" | **Har order ke sath SL aur TP broker par lage hue hain.** Bot, terminal, VPS teeno mar jayein tab bhi position broker khud band karega. Yeh sab se ahem safety hai aur pehle se bani hui hai |

To amli tor par bot 24/5 chalta rahega. Bas terminal ka process zinda rehna
chahiye — **use dekhne ke liye kisi ka baithna zaroori nahi.**

---

## 1. VPS par kya chahiye

- **Windows** VPS (Server 2019+ ya Windows 10/11). Linux par MT5 terminal
  nahi chalta (Wine ke saath chalta hai lekin nazuk rehta hai — mashwara nahi)
- **2 GB RAM** kam az kam, 4 GB behtar (terminal ~500 MB leta hai)
- **Python 3.12** — https://www.python.org/downloads/
  install karte waqt **"Add Python to PATH"** zaroor tick karein
- VPS **broker ke server ke qareeb** ho to slippage kam hoti hai. Exness ke
  liye Europe (London/Amsterdam) achha rehta hai

---

## 2. Files le jayein

```powershell
cd C:\
git clone <aap-ki-repo-ka-URL> gold-2.6-bot
cd C:\gold-2.6-bot
```

`.env` repo mein **nahi** hai (gitignored) — woh aap alag se banayenge, step 4.

---

## 3. MT5 terminal install karein

1. Exness se MT5 download karke install karein — **`C:\MT5-Gold26`** mein
   (default `Program Files` mein nahi, taake aage doosre bots se na takraye)
2. Ek baar khol kar apne demo account se login karein, taake terminal server
   ka address yaad rakh le
3. Market Watch mein **XAUUSD** dikh raha hai confirm karein
   (right-click → Show All agar na dikhe)
4. Tools → Options → Charts → **"Max bars in chart"** ko `unlimited` kar dein,
   warna backtest ke liye history kam milegi
5. Tools → Options → Expert Advisors → **"Allow algorithmic trading"** tick karein

> **Ek se zyada bots?** Har bot ka **apna alag terminal folder** hona chahiye.
> Ek terminal ek waqt mein sirf **ek** account par login rehta hai, aur jo bhi
> aakhir mein `initialize(login=...)` call kare wohi jeet ta hai. Isi wajah se
> is machine par `mt5_beast_bot` ek ghante tak apna symbol nahi dhoondh saka tha.

---

## 4. Python environment aur credentials

```powershell
cd C:\gold-2.6-bot
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

copy .env.example .env
notepad .env
```

`.env` mein bharen:

```
MT5_TERMINAL_PATH=C:\MT5-Gold26\terminal64.exe
MT5_LOGIN=<account number>
MT5_PASSWORD=<password>
MT5_SERVER=<e.g. Exness-MT5Trial16>

TELEGRAM_BOT_TOKEN=<BotFather se>
TELEGRAM_CHAT_ID=<neeche dekhein>
```

**Telegram chat ID:** apne bot ko Telegram par koi bhi message bhejein, phir

```powershell
.venv\Scripts\python.exe telegram_bot.py
```

Woh chat ID print kar dega. Telegram set na ho to bhi bot **chalega** — sirf
alerts band rahenge. Alerts ka na hona trading rokne ki wajah nahi honi chahiye.

### Sab kuch theek hai? Test karein

```powershell
.venv\Scripts\python.exe -m pytest tests\ -q          # 61 tests
.venv\Scripts\python.exe backtester.py --months 12    # data + engine + costs
```

Backtest chal gaya to matlab connection, symbol, history, sizing sab kaam kar
rahe hain — **aur ek bhi order nahi gaya**, backtester kabhi order nahi bhejta.

---

## 5. Auto-start — reboot ke baad khud chalu ho jaye

Yeh hissa dhyan se, kyunke Windows par ek jaal hai.

**MT5 terminal ek GUI app hai — usay interactive session chahiye.** Task
Scheduler ka *"Run whether user is logged on or not"* option session 0 mein
chalata hai jahan desktop hota hi nahi, aur terminal wahan chal nahi payega.

**Sahi tareeqa: auto-logon + "At log on" task.**

### 5a. Auto-logon on karein

```powershell
# Administrator PowerShell mein
$key = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
Set-ItemProperty $key "AutoAdminLogon" "1"
Set-ItemProperty $key "DefaultUserName" "Administrator"
Set-ItemProperty $key "DefaultPassword" "<VPS ka password>"
```

> Password registry mein plain text mein jata hai. VPS par yeh aam amal hai,
> lekin jaan lein ke aisa hai. Behtar option: Sysinternals **AutoLogon** tool,
> jo usay encrypted LSA secret mein rakhta hai.

### 5b. Bot ka task banayein

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
           -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File C:\gold-2.6-bot\run.ps1"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
            -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "Gold26Bot" -Action $action -Trigger $trigger `
    -Settings $settings -RunLevel Highest
```

`ExecutionTimeLimit` zero rakhna zaroori hai — warna Windows 3 din baad task
khud band kar deta hai.

### 5c. Test karein

VPS reboot karein. Wapas RDP karein aur dekhein:

```powershell
Get-ScheduledTask Gold26Bot | Get-ScheduledTaskInfo
Get-Content C:\gold-2.6-bot\logs\bot.log -Tail 30
```

---

## 6. Chalana aur rokna

```powershell
# haath se chalayein (supervisor ke sath — crash par khud restart)
powershell -ExecutionPolicy Bypass -File C:\gold-2.6-bot\run.ps1

# bina supervisor ke, debugging ke liye
.venv\Scripts\python.exe main.py

# rokein
Ctrl+C          # nikalte waqt apne resting orders khud cancel kar deta hai
Stop-ScheduledTask -TaskName Gold26Bot
```

**RDP se nikalte waqt "Disconnect" karein, "Sign out" nahi.** Sign out session
maar deta hai aur uske sath terminal aur bot dono.

---

## 7. Rozana kya dekhein

```powershell
Get-Content C:\gold-2.6-bot\logs\bot.log -Tail 40      # kya ho raha hai
Get-Content C:\gold-2.6-bot\runtime\journal.csv        # har trade
type C:\gold-2.6-bot\runtime\risk_state.json           # daily P&L, peak equity
```

**Kill switch:** agar `runtime\KILL_SWITCH` file ban jaye to bot ne khud ko rok
diya hai (10% drawdown, ya lagataar 5 errors). File ke andar wajah likhi hoti
hai. **Woh file khud nahi mitti — pehle samjhein kyun fire hui, phir delete
karein.** Aisi limit jo khud reset ho jaye, limit hoti hi nahi.

---

## 8. Live jane se pehle (Phase 8)

`.env` mein do cheezein — **aur woh sirf aap set karenge**:

```
LIVE_TRADING_ENABLED=true
LIVE_CONFIRMATION_PHRASE=<koi lambi phrase>
```

Code mein koi raasta nahi jo in ke baghair live order bheje — dekhein
`assert_live_unlocked()` [config.py](config.py). Main in mein se koi bhi set
nahi karunga.

Live jane se pehle demo par kam az kam **3 mahine ya ~100 trades** chala lein,
aur `journal.csv` ke numbers backtest se milayein: win rate ~34%, mean R ~0,
hafte mein ~5 trades. Agar live numbers bohot alag aayein — to backtest aur
haqeeqat ke darmiyan koi farq hai jo abhi tak nahi pakda gaya, aur **wohi sab se
qeemti cheez hogi jo demo bata sakta hai.**
