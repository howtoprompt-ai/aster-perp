# 🚀 Aster Soft  

### Powerful automation software for [Aster](https://www.asterdex.com/) trading.  
Aster Soft helps you execute advanced trading strategies with precision and flexibility.  

---

## ⚡ Key Features  

🔹 **4 Operation Modes**  
1. **Futures Market** – instant entry and exit using market orders.  
2. **Futures Limits** – configurable limit-order trading with smart entry/exit logic.  
3. **Pair Futures Limits (Delta-Neutral)** – multi-account strategy with opposite positions for delta-neutral execution.  
4. **Cancel All Orders & Positions** – one-click cancellation of all open orders or closing of current positions.  

---

## 🎯 Smart Trading Logic  

### 🔸 Futures Limits  
- **Configurable entries** – set your own entry price or auto-use the best order book price.  
- **Targeted exits** – close at *entry price + offset* (absolute or %).  
  - Example: Buy at $3000 → auto-close at $3002 if offset = +$2.  
- **Adaptive order handling** –  
  - Waits for a set time (default: 1 min).  
  - If unfilled, automatically re-prices to current market level.  
- **Custom trade direction** – run longs only, shorts only, or both.  

### 🔸 Pair Futures Limits (Delta-Neutral)  
- Opens a **limit order** from the account with the largest position.  
- Once filled, **all other accounts** instantly mirror the trade with market orders at the same price.  
- Makes account synchronization seamless and **harder to detect**.  

---

## ⚙️ Setup  

1. Fill input files in the `input_data` folder:  
   - **`apikeys.txt`** → `api_key:api_secret` or `label:api_key:api_secret`  
     - If no label is provided, accounts are auto-named (*Account 1, Account 2…*).  
   - **`proxies.txt`** → add your proxy list (examples inside).  
2. Adjust settings in **`settings.py`** (trade size, number of trades, price logic, etc.).  

---

## ▶️ Getting Started  

1. Install dependencies:  
   ```bash
   pip install -r requirements.txt
2. Run the software:
   ```bash
   python main.py
   ```
3. Create a database (Create Database → New Database).
4. Select and launch your desired mode.
