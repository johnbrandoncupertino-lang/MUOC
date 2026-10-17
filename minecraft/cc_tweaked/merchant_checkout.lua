-- MineBank merchant checkout: always-on, customer-facing native touchscreen flow.
-- Keep this computer and its merchant token under the merchant's control.
local runningProgram = shell.getRunningProgram()
local programDir = fs.getDir(runningProgram)
local ui = dofile(fs.combine(programDir, "minebank_ui.lua"))
ui.useTarget("terminal")
local CONFIG = fs.combine(programDir, "minebank_merchant.json")
local REVIEW = fs.combine(programDir, "minebank_merchant_review.json")
local SALES = fs.combine(programDir, "minebank_merchant_sales.json")
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

local function recordSale(checkout, payment)
  local sales = readJSON(SALES) or {}
  if type(sales) ~= "table" then sales = {} end
  local id = tostring(checkout.checkout_id or "")
  for _, existing in ipairs(sales) do
    if tostring(existing.checkout_id or "") == id then return true end
  end
  table.insert(sales, 1, {
    checkout_id = id,
    amount = tonumber(checkout.amount) or 0,
    description = tostring(checkout.description or "Purchase"),
    merchant = tostring(checkout.merchant or ""),
    funding_source = tostring(payment.funding_source or "UNKNOWN"),
    transaction_id = tostring(payment.transaction_id or ""),
    status = "PAID",
    recorded_at = os.epoch("utc"),
  })
  while #sales > 100 do table.remove(sales) end
  return saveJSON(SALES, sales)
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
      recordSale(checkout,{transaction_id=status.transaction_id,funding_source=funding})
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
  recordSale(checkout,paid)
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

local function runSale(cfg,preset)
  ui.useTarget("terminal")
  ui.API_BASE=cfg.api_base
  if not resolvePriorCheckout(cfg) then return end
  ui.clear(); ui.header("MERCHANT CHECKOUT","Cashier workspace - customer display is separate")
  local amount
  if preset then amount=tonumber(preset.amount)
  else amount=tonumber(ui.prompt("Purchase amount in Emeralds")) end
  if not amount or amount%1~=0 or amount<1 or amount>1000000 then
    ui.message("Enter a whole Emerald amount from 1 to 1,000,000.",false); ui.pause()
    return
  end
  amount=math.floor(amount)
  local description
  if preset then description=tostring(preset.description or preset.name or "Quick checkout")
  else description=ui.prompt("Purchase description",cfg.default_description or "Shop purchase") end
  if description==nil then return end
  description=tostring(description):sub(1,200)
  local checkout,err=createCheckout(cfg,amount,description)
  if not checkout then
    ui.clear(); ui.header("CHECKOUT UNAVAILABLE")
    ui.message(tostring(err),false); ui.pause()
    return
  end
  ui.useTarget("terminal")
  ui.clear(); ui.header("CUSTOMER CHECKOUT","Waiting for customer on the monitor")
  ui.message("Checkout ID: "..tostring(checkout.checkout_id),true)
  ui.message("Amount due: "..tostring(checkout.amount).." Emerald",true)
  ui.message("Description: "..tostring(checkout.description or description),true)
  ui.message("The customer is using the separate touchscreen display. Do not open another checkout until this one is resolved.",true)
  local success,detail=customerPayment(cfg,checkout)
  if not success and not readJSON(REVIEW) then
    -- A user cancellation or definitive rejection leaves no ambiguous charge;
    -- close the open checkout so it cannot be accidentally paid later.
    local cancelled,cancelErr=ui.api("/merchant/checkouts/"..tostring(checkout.checkout_id).."/cancel",
      "POST",cfg.merchant_token,{})
    if not cancelled then
      ui.useTarget("terminal")
      ui.message("The checkout could not be closed automatically: "..tostring(cancelErr or "unknown error")..". Verify its status before reuse.",false)
    end
  end
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

local function showRecentSales()
  ui.useTarget("terminal")
  local sales=readJSON(SALES) or {}
  if #sales==0 then
    ui.clear(); ui.header("SALES HISTORY","No locally recorded completed sales")
    ui.message("Completed sales will appear here after MineBank confirms payment.",true)
    ui.pause()
    return
  end
  local opts={}
  for _,sale in ipairs(sales) do
    opts[#opts+1]="#"..tostring(sale.checkout_id).." - "..tostring(sale.amount).." Emerald - "..tostring(sale.status or "PAID")
  end
  local index=ui.choose("RECENT SALES",opts,"Local history - up to 100 confirmed sales.")
  if index and sales[index] then
    local sale=sales[index]
    ui.clear(); ui.header("SALE RECEIPT","MineBank confirmed payment")
    ui.message("Checkout: "..tostring(sale.checkout_id),true)
    ui.message("Amount: "..tostring(sale.amount).." Emerald",true)
    ui.message("Description: "..tostring(sale.description),true)
    ui.message("Funding: "..tostring(sale.funding_source),true)
    ui.message("Transaction: "..tostring(sale.transaction_id),true)
    ui.message("Recorded: "..tostring(sale.recorded_at),true)
    ui.pause()
  end
end

local function reviewCheckout(cfg)
  ui.useTarget("terminal")
  local id=ui.prompt("Checkout ID to inspect")
  if not id or not id:match("^%d+$") then ui.message("Enter a numeric checkout ID.",false); ui.pause(); return end
  local status,err=ui.api("/merchant/checkouts/"..id,"GET",cfg.merchant_token)
  if not status then ui.message("Status check failed: "..tostring(err),false); ui.pause(); return end
  ui.clear(); ui.header("CHECKOUT STATUS","Authoritative status from MineBank")
  ui.message("Checkout: "..tostring(status.checkout_id),true)
  ui.message("Status: "..tostring(status.status),status.status=="PAID")
  ui.message("Amount: "..tostring(status.amount).." Emerald",true)
  ui.message("Description: "..tostring(status.description),true)
  ui.message("Transaction: "..tostring(status.transaction_id or "none"),true)
  if status.status=="OPEN" then
    local confirm=ui.choose("CANCEL OPEN CHECKOUT",{"Cancel this unpaid checkout","Keep it open"})
    if confirm==1 then
      local result,cancelErr=ui.api("/merchant/checkouts/"..id.."/cancel","POST",cfg.merchant_token,{})
      ui.message(result and "Checkout cancelled. It can no longer be paid." or ("Cancellation failed: "..tostring(cancelErr)),not not result)
    end
  end
  ui.pause()
end

local function showRecentCheckouts(cfg)
  ui.useTarget("terminal")
  local result,err=ui.api("/merchant/checkouts","GET",cfg.merchant_token)
  if not result then
    ui.clear(); ui.header("CHECKOUT HISTORY","MineBank could not load recent checkouts")
    ui.message(tostring(err),false); ui.pause()
    return
  end
  local rows=result.checkouts or {}
  if #rows==0 then
    ui.clear(); ui.header("CHECKOUT HISTORY","No recent checkouts")
    ui.message("New checkouts will appear here after the cashier creates them.",true); ui.pause()
    return
  end
  local options={}
  for _,row in ipairs(rows) do
    options[#options+1]="#"..tostring(row.checkout_id).." | "..tostring(row.amount).." E | "..tostring(row.status)
  end
  local index=ui.choose("MINEBANK CHECKOUT HISTORY",options,"Recent server-side checkout records. Paid status comes from MineBank.")
  if not index or not rows[index] then return end
  local row=rows[index]
  ui.clear(); ui.header("CHECKOUT RECORD","Authoritative MineBank record")
  ui.message("Checkout: "..tostring(row.checkout_id),true)
  ui.message("Status: "..tostring(row.status),row.status=="PAID")
  ui.message("Amount: "..tostring(row.amount).." Emerald",true)
  ui.message("Description: "..tostring(row.description),true)
  ui.message("Transaction: "..tostring(row.transaction_id or "none"),true)
  ui.message("Created: "..tostring(row.created_at or "unknown"),true)
  ui.message("Paid: "..tostring(row.paid_at or "not paid"),true)
  if row.status=="OPEN" then
    local choice=ui.choose("UNPAID CHECKOUT",{"Cancel this checkout","Keep checkout open"})
    if choice==1 then
      local cancelled,cancelErr=ui.api("/merchant/checkouts/"..tostring(row.checkout_id).."/cancel","POST",cfg.merchant_token,{})
      ui.message(cancelled and "Checkout cancelled; it cannot be paid now." or ("Cancellation failed: "..tostring(cancelErr)),not not cancelled)
    end
  end
  ui.pause()
end

local function cashierSettings(cfg)
  while true do
    ui.useTarget("terminal")
    local choice=ui.choose("CASHIER SETTINGS",{
      "Change API address","Rotate merchant token","Set default description","Manage quick charges","Return to register"
    },"These settings affect only this cashier computer.")
    if not choice or choice==5 then
      if not saveConfig(cfg) then ui.message("Configuration could not be saved.",false); ui.pause() end
      return
    elseif choice==1 then
      local api=ui.prompt("MineBank HTTPS API base",cfg.api_base)
      if api and api:match("^https://") then cfg.api_base=api:gsub("/+$",""); ui.API_BASE=cfg.api_base
      else ui.message("API address must use HTTPS.",false) end
    elseif choice==2 then
      local token=ui.secret("New merchant token (mbmerchant_...): ")
      if token and token:match("^mbmerchant_") and #token>=40 then cfg.merchant_token=token
      else ui.message("Token format is invalid. Existing token unchanged.",false) end
    elseif choice==3 then
      local desc=ui.prompt("Default purchase description",cfg.default_description or "Shop purchase")
      if desc and desc:match("%S") then cfg.default_description=desc:sub(1,200) end
    elseif choice==4 then
      local presets={}
      for i=1,12 do
        local old=(cfg.presets or {})[i]
        local name=ui.prompt("Quick charge "..i.." name (blank to finish)",old and old.name or "")
        if not name or name=="" then break end
        local amount=tonumber(ui.prompt("Amount in Emeralds",tostring(old and old.amount or 1)))
        local description=ui.prompt("Receipt description",old and old.description or name)
        if not amount or amount%1~=0 or amount<1 or amount>1000000 then
          ui.message("Invalid amount. Quick charge skipped.",false)
        else presets[#presets+1]={name=name:sub(1,80),amount=amount,description=tostring(description or name):sub(1,200)} end
      end
      if #presets>0 then cfg.presets=presets end
    end
    if not saveConfig(cfg) then ui.message("Could not save cashier settings.",false); ui.pause() end
  end
end

while true do
  ui.useTarget("terminal")
  local options={"New checkout","Recent sales","Recent MineBank checkouts","Inspect/cancel checkout","Cashier settings"}
  local quickStart=6
  for _,preset in ipairs(cfg.presets or {}) do options[#options+1]="Quick: "..tostring(preset.name).." - "..tostring(preset.amount).." Emerald" end
  options[#options+1]="Exit register"
  local choice=ui.choose("MERCHANT REGISTER",options,"Customer checkout is shown only on the separate monitor.")
  if not choice then
    -- A physical keyboard interrupt may stop the process; touchscreen customers
    -- never see this register menu.
  elseif choice==1 then
    local ok,err=pcall(runSale,cfg,nil)
    if not ok then
      ui.useTarget("terminal"); ui.clear(); ui.header("CHECKOUT RECOVERED")
      ui.message("A screen error was caught. Check the original checkout status before retrying any payment.",false)
      ui.message(tostring(err),false); ui.pause()
    end
  elseif choice==2 then showRecentSales()
  elseif choice==3 then showRecentCheckouts(cfg)
  elseif choice==4 then reviewCheckout(cfg)
  elseif choice==5 then cashierSettings(cfg)
  elseif choice>=quickStart and choice<quickStart+#(cfg.presets or {}) then
    local preset=(cfg.presets or {})[choice-quickStart+1]
    local ok,err=pcall(runSale,cfg,preset)
    if not ok then
      ui.useTarget("terminal"); ui.clear(); ui.header("CHECKOUT RECOVERED")
      ui.message("A screen error was caught. Check the original checkout status before retrying any payment.",false)
      ui.message(tostring(err),false); ui.pause()
    end
  else
    break
  end
  if ui.hasMonitor() then ui.kioskIdle() end
end
