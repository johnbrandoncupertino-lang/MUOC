-- MineBank merchant checkout: always-on, customer-facing Basalt touch flow.
-- Keep this computer and its merchant token under the merchant's control.
local runningProgram = shell.getRunningProgram()
local programDir = fs.getDir(runningProgram)
local ui = dofile(fs.combine(programDir, "minebank_ui.lua"))
local CONFIG = fs.combine(programDir, "minebank_merchant.json")
local DEFAULT_API = "https://minebanksecurearea.vercel.app/api/v1/minecraft"

local function loadConfig()
  if not fs.exists(CONFIG) then return nil end
  local h=fs.open(CONFIG,"r")
  if not h then return nil end
  local raw=h.readAll(); h.close()
  local ok,cfg=pcall(textutils.unserializeJSON,raw)
  if ok and type(cfg)=="table" then return cfg end
  return nil
end

local function saveConfig(cfg)
  local h=fs.open(CONFIG..".tmp","w")
  if not h then return false end
  h.write(textutils.serializeJSON(cfg)); h.close()
  if fs.exists(CONFIG) then fs.delete(CONFIG) end
  fs.move(CONFIG..".tmp",CONFIG)
  return true
end

local function setupWizard()
  while true do
    ui.clear(); ui.header("MERCHANT SETUP","One-time configuration")
    ui.message("Configure the merchant API token. It is stored locally on this trusted computer, never on customer cards.",true)
    ui.pause()
    local api=ui.prompt("MineBank HTTPS API base",DEFAULT_API)
    if not api or not api:match("^https://") then
      ui.message("The API address must use HTTPS.",false); ui.pause()
    else
      api=api:gsub("/+$","")
      local token=ui.prompt("Merchant token (mbmerchant_…)")
      if token and token:match("^mbmerchant_") and #token>=40 then
        local cfg={api_base=api,merchant_token=token}
        if saveConfig(cfg) then return cfg end
        ui.message("Could not save the configuration. Check disk space and file permissions.",false); ui.pause()
      else
        ui.message("That does not look like a valid MineBank merchant token.",false); ui.pause()
      end
    end
  end
end

local function readCard()
  local card,err=ui.card()
  return card,err
end

local function waitForCard()
  while true do
    local card=readCard()
    if card then return card end
    ui.clear(); ui.header("MINEBANK PAYMENT","Waiting for customer card")
    ui.message("Insert your MineBank card floppy into the reader. This terminal stays ready without a card.",true)
    local timer=os.startTimer(1)
    while true do
      local event=os.pullEventRaw()
      if event=="timer" then break end
      if event=="terminate" then pcall(os.cancelTimer,timer); return nil,"cancelled" end
    end
  end
end

local function createCheckout(cfg,amount,description)
  ui.API_BASE=cfg.api_base
  return ui.api("/merchant/checkouts","POST",cfg.merchant_token,
    {amount=amount,description=description},ui.key("merchant-checkout"))
end

local function customerPayment(cfg,checkout)
  ui.clear(); ui.header("MINEBANK PAYMENT","Secure customer checkout")
  local confirmed=ui.choose("CONFIRM PURCHASE",{
    "Pay "..tostring(checkout.amount).." Emerald",
    "Cancel purchase"
  },"Merchant: "..tostring(checkout.merchant or "MineBank merchant").." | "..tostring(checkout.description or "Purchase"))
  if confirmed~=1 then return false,"Customer cancelled. No payment was sent." end

  ui.clear(); ui.header("MINEBANK PAYMENT","Card required for this purchase")
  ui.message("The amount is fixed by MineBank. Insert your card, then enter your Wallet PIN on this touchscreen.",true)
  local card,cardErr=waitForCard()
  if not card then return false,cardErr end

  local pin=ui.secret("Enter your six-digit Wallet PIN")
  if not pin then return false,"Payment cancelled." end
  if not pin:match("^%d%d%d%d%d%d$") then return false,"Wallet PIN must contain six digits." end

  local source=ui.choose("CHOOSE PAYMENT METHOD",{
    "MineBank Balance",
    "CashLine credit",
    "Cancel"
  },"Amount: "..tostring(checkout.amount).." Emerald")
  if source==3 or not source then return false,"Payment cancelled." end
  local funding=source==1 and "BALANCE" or "CASHLINE"

  local final=ui.choose("AUTHORISE PAYMENT",{
    "Authorise "..tostring(checkout.amount).." Emerald",
    "Cancel"
  },"Pay with "..(funding=="BALANCE" and "MineBank Balance" or "CashLine").."?")
  if final~=1 then return false,"Payment cancelled. No payment was sent." end

  ui.clear(); ui.header("MINEBANK PAYMENT","Checking with MineBank")
  ui.message("Verifying card, PIN, account status, limits and available funds. Do not remove the card.",true)
  local paid,err=ui.api("/payments","POST",card.credential,
    {checkout_id=tostring(checkout.checkout_id),wallet_pin=pin,funding_source=funding},
    "checkout-"..tostring(checkout.checkout_id).."-"..funding)
  pin=nil
  if not paid then
    ui.clear(); ui.header("PAYMENT NOT CONFIRMED")
    ui.message(tostring(err)..". Do not retry blindly; check the original checkout status.",false)
    ui.pause()
    local status=ui.api("/merchant/checkouts/"..tostring(checkout.checkout_id),"GET",cfg.merchant_token)
    if status and status.status=="PAID" then
      ui.clear(); ui.header("PAYMENT COMPLETED")
      ui.message("MineBank confirms this checkout is paid. Transaction: "..tostring(status.transaction_id),true)
      ui.pause()
      return true,"Payment confirmed by checkout status."
    end
    return false,"Payment not confirmed."
  end
  if paid.status~="COMPLETED" then return false,"MineBank did not confirm a completed payment. No success is shown." end
  ui.clear(); ui.header("PAYMENT APPROVED","Thank you for banking with MineBank")
  ui.message(tostring(paid.amount).." Emerald paid successfully.",true)
  ui.message("Transaction: "..tostring(paid.transaction_id),true)
  ui.pause()
  return true,"Payment completed."
end

local function runSale(cfg)
  ui.API_BASE=cfg.api_base
  ui.clear(); ui.header("MERCHANT CHECKOUT","Ready for the next customer")
  local amountText=ui.prompt("Purchase amount in Emeralds")
  local amount=tonumber(amountText)
  if not amount or amount%1~=0 or amount<1 or amount>1000000 then
    ui.message("Enter a whole Emerald amount from 1 to 1,000,000.",false); ui.pause()
    return
  end
  amount=math.floor(amount)
  local description=ui.prompt("Purchase description","Shop purchase")
  if description==nil then return end
  description=tostring(description):sub(1,200)
  local checkout,err=createCheckout(cfg,amount,description)
  if not checkout then
    ui.clear(); ui.header("CHECKOUT UNAVAILABLE")
    ui.message(tostring(err),false); ui.pause()
    return
  end
  customerPayment(cfg,checkout)
end

if not http then
  ui.clear(); ui.header("SETUP REQUIRED")
  ui.message("Enable the CC:Tweaked HTTP API in the computer configuration, then restart this program.",false)
  ui.pause()
  return
end

local cfg=loadConfig()
if not cfg or not cfg.api_base or not cfg.merchant_token or not cfg.merchant_token:match("^mbmerchant_") then
  cfg=setupWizard()
end

while true do
  local ok,err=pcall(runSale,cfg)
  if not ok then
    ui.clear(); ui.header("CHECKOUT RECOVERED")
    ui.message("A screen error was caught; the program remains running. Check the original checkout status before retrying any payment.",false)
    ui.message(tostring(err),false)
    ui.pause()
  end
end
