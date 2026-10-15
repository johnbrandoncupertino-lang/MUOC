-- MineBank merchant checkout: always-on, customer-facing Basalt touch flow.
-- Keep this computer and its merchant token under the merchant's control.
local runningProgram = shell.getRunningProgram()
local programDir = fs.getDir(runningProgram)
local ui = dofile(fs.combine(programDir, "minebank_ui.lua"))
ui.useTarget("terminal")
local CONFIG = fs.combine(programDir, "minebank_merchant.json")
local REVIEW = fs.combine(programDir, "minebank_merchant_review.json")
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

local function readJSON(path)
  if not fs.exists(path) then return nil end
  local h=fs.open(path,"r")
  if not h then return nil end
  local raw=h.readAll(); h.close()
  local ok,value=pcall(textutils.unserializeJSON,raw)
  if ok and type(value)=="table" then return value end
  return nil
end

local function saveJSON(path,value)
  local tmp=path..".tmp"
  local h=fs.open(tmp,"w")
  if not h then return false end
  local ok=pcall(function() h.write(textutils.serializeJSON(value)); h.close() end)
  if not ok then pcall(function() h.close() end); pcall(fs.delete,tmp); return false end
  if fs.exists(path) then pcall(fs.delete,path) end
  local moved=pcall(fs.move,tmp,path)
  if not moved then pcall(fs.delete,tmp); return false end
  return true
end

local function clearReview()
  pcall(function() if fs.exists(REVIEW) then fs.delete(REVIEW) end end)
  pcall(function() if fs.exists(REVIEW..".tmp") then fs.delete(REVIEW..".tmp") end end)
end

local function saveReview(value)
  return saveJSON(REVIEW,value)
end

local function waitForCard()
  while true do
    local card=readCard()
    if card then return card end
    local choice=ui.choose("CARD REQUIRED",{
      "I have inserted the MineBank card",
      "Cancel this purchase",
    },"Insert the customer's MineBank floppy card into the drive, then touch the first action. No card is required while the terminal is idle.")
    if choice~=1 then return nil,"Customer cancelled before authorisation." end
  end
end

local function createCheckout(cfg,amount,description)
  ui.API_BASE=cfg.api_base
  return ui.api("/merchant/checkouts","POST",cfg.merchant_token,
    {amount=amount,description=description},ui.key("merchant-checkout"))
end

local function customerPayment(cfg,checkout)
  ui.useTarget("monitor")
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
  local paymentKey="checkout-"..tostring(checkout.checkout_id).."-"..funding
  local paid,err,httpStatus=ui.api("/payments","POST",card.credential,
    {checkout_id=tostring(checkout.checkout_id),wallet_pin=pin,funding_source=funding},
    paymentKey)
  pin=nil
  if not paid then
    -- A 400/401/403/404/422 is a definitive rejection from the endpoint. It
    -- cannot be mistaken for a transient transport failure, and no payment POST
    -- is resent. CashLine eligibility/limit rejections arrive as HTTP 422.
    if httpStatus and (httpStatus==400 or httpStatus==401 or httpStatus==403 or httpStatus==404 or httpStatus==422) then
      ui.clear(); ui.header("PAYMENT REJECTED","MineBank did not authorise this payment")
      ui.message("MineBank response (HTTP "..tostring(httpStatus).."): "..tostring(err),false)
      ui.message("The payment was rejected; no debit or merchant credit was committed. Return to the cashier and choose another available payment method.",true)
      ui.pause()
      return false,"Payment rejected by MineBank (HTTP "..tostring(httpStatus).."). No money was moved; use another payment method or resolve the account restriction."
    end
    -- A real 409, a timeout, or a server/network failure can leave the caller
    -- uncertain about the original operation. Never send another POST: only
    -- GET the same checkout to reconcile its authoritative state.
    ui.clear(); ui.header("PAYMENT REQUIRES REVIEW","No automatic payment retry will be sent")
    ui.message("MineBank response"..(httpStatus and (" (HTTP "..tostring(httpStatus)..")") or "")..": "..tostring(err),false)
    ui.message("Checking the original checkout status. Keep this checkout ID and do not repeat the payment.",true)
    ui.pause()
    local status,statusErr,statusCode=ui.api("/merchant/checkouts/"..tostring(checkout.checkout_id),"GET",cfg.merchant_token)
    if status and status.status=="PAID" then
      clearReview()
      ui.clear(); ui.header("PAYMENT COMPLETED","Confirmed by the original checkout")
      ui.message("MineBank confirms the existing checkout is paid.",true)
      ui.message("Transaction: "..tostring(status.transaction_id),true)
      ui.pause()
      return true,"Payment confirmed by checkout status."
    end
    local review={
      checkout_id=tostring(checkout.checkout_id),
      amount=tonumber(checkout.amount) or 0,
      funding_source=funding,
      idempotency_key=paymentKey,
      payment_http_status=httpStatus,
      payment_error=tostring(err or "Unknown payment error"),
      checkout_status=status and tostring(status.status) or "UNKNOWN",
      status_error=statusErr and tostring(statusErr) or nil,
      status_http_status=statusCode,
      recorded_at=os.epoch("utc"),
    }
    if not saveReview(review) then
      ui.message("CRITICAL: could not save local reconciliation record. Do not open another sale until this checkout is manually checked.",false)
    end
    ui.clear(); ui.header("PAYMENT NOT CONFIRMED","Original checkout retained for reconciliation")
    ui.message("Checkout: "..tostring(checkout.checkout_id).." • HTTP "..tostring(httpStatus or "unknown"),false)
    ui.message("Payment result: "..tostring(err or "unknown"),false)
    if status then
      ui.message("Current checkout status: "..tostring(status.status)..". No second payment request was sent.",false)
    else
      ui.message("Checkout status could not be verified: "..tostring(statusErr or "unknown")..". Outcome must be reconciled before another sale.",false)
    end
    ui.pause()
    return false,"Payment requires reconciliation."
  end
  if paid.status~="COMPLETED" then
    saveReview({
      checkout_id=tostring(checkout.checkout_id),amount=tonumber(checkout.amount) or 0,
      funding_source=funding,idempotency_key=paymentKey,
      payment_http_status=200,payment_error="Unexpected response status: "..tostring(paid.status),
      checkout_status=tostring(paid.status),recorded_at=os.epoch("utc"),
    })
    ui.clear(); ui.header("PAYMENT REQUIRES REVIEW","MineBank did not confirm COMPLETED")
    ui.message("No success is shown. Reconcile checkout "..tostring(checkout.checkout_id).." before accepting another payment.",false)
    ui.pause()
    return false,"Unexpected payment status; reconciliation required."
  end
  clearReview()
  ui.clear(); ui.header("PAYMENT APPROVED","Thank you for banking with MineBank")
  ui.message(tostring(paid.amount).." Emerald paid successfully.",true)
  ui.message("Funding source: "..tostring(paid.funding_source or funding),true)
  ui.message("Transaction: "..tostring(paid.transaction_id),true)
  ui.pause()
  return true,"Payment completed."
end

local function resolvePriorCheckout(cfg)
  local review=readJSON(REVIEW)
  if not review then return true end
  local checkoutId=tostring(review.checkout_id or "")
  ui.useTarget("terminal")
  local status,statusErr=ui.api("/merchant/checkouts/"..checkoutId,"GET",cfg.merchant_token)
  if status and status.status=="PAID" then
    clearReview()
    ui.clear(); ui.header("PRIOR CHECKOUT RECONCILED","Payment already completed")
    ui.message("Checkout "..checkoutId.." is PAID. Transaction: "..tostring(status.transaction_id),true)
    ui.pause()
    return true
  end
  local options={"Check original checkout again","Acknowledge rejection and allow a new sale","Keep the register locked"}
  local choice=ui.choose("CHECKOUT REVIEW REQUIRED",options,
    "Previous checkout "..checkoutId.." • "..tostring(status and status.status or "status unavailable"))
  if choice==1 then
    local refreshed,refreshErr=ui.api("/merchant/checkouts/"..checkoutId,"GET",cfg.merchant_token)
    if refreshed and refreshed.status=="PAID" then
      clearReview()
      ui.clear(); ui.header("PRIOR PAYMENT CONFIRMED")
      ui.message("The original checkout is already paid. Transaction: "..tostring(refreshed.transaction_id),true)
      ui.pause()
      return true
    end
    ui.message("Original checkout still not confirmed as paid. No payment request was resent. "..tostring(refreshErr or (refreshed and refreshed.status) or statusErr or "Unknown status"),false)
    ui.pause()
    return false
  elseif choice==2 then
    if not status then
      ui.clear(); ui.header("REGISTER REMAINS LOCKED","The original checkout cannot be verified")
      ui.message("MineBank status check failed: "..tostring(statusErr or "unknown error")..". Do not open another sale until connectivity is restored and the original checkout is reconciled.",false)
      ui.pause()
      return false
    end
    clearReview()
    ui.clear(); ui.header("CHECKOUT ABANDONED","No payment was automatically retried")
    ui.message("The cashier explicitly released checkout "..checkoutId.." after reviewing status "..tostring(status.status)..".",true)
    ui.pause()
    return true
  end
  return false
end

local function runSale(cfg)
  ui.useTarget("terminal")
  ui.API_BASE=cfg.api_base
  if not resolvePriorCheckout(cfg) then return end
  ui.clear(); ui.header("MERCHANT CHECKOUT","Cashier workspace • customer display is separate")
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
  local success,detail=customerPayment(cfg,checkout)
  ui.useTarget("terminal")
  ui.kioskIdle()
  ui.useTarget("terminal")
  ui.clear(); ui.header(success and "SALE COMPLETE" or "SALE CLOSED","Cashier summary")
  ui.message(tostring(detail or (success and "Payment completed." or "Payment was not completed.")),success)
  ui.pause()
end

if not http then
  ui.clear(); ui.header("SETUP REQUIRED")
  ui.message("Enable the CC:Tweaked HTTP API in the computer configuration, then restart this program.",false)
  ui.pause()
  return
end

ui.useTarget("terminal")
local cfg=loadConfig()
if not cfg or not cfg.api_base or not cfg.merchant_token or not cfg.merchant_token:match("^mbmerchant_") then
  cfg=setupWizard()
end
if ui.hasMonitor() then ui.kioskIdle() end

while true do
  local ok,err=pcall(runSale,cfg)
  if not ok then
    ui.useTarget("terminal")
    ui.clear(); ui.header("CHECKOUT RECOVERED")
    ui.message("A screen error was caught; the program remains running. Check the original checkout status before retrying any payment.",false)
    ui.message(tostring(err),false)
    ui.pause()
    if ui.hasMonitor() then ui.kioskIdle() end
  end
end
