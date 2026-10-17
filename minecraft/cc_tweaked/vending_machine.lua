-- MineBank automated vending terminal for CC:Tweaked.
-- Requires a trusted merchant token, a stock inventory and a separate output inventory.
-- A paid order is journalled before dispensing; interrupted physical delivery is never replayed automatically.

local runningProgram = shell.getRunningProgram()
local programDir = fs.getDir(runningProgram)
local uiPath = fs.combine(programDir, "minebank_ui.lua")
local ui = dofile(uiPath)
ui.useTarget("terminal")
local function print(...)
  local values = {}
  for i = 1, select("#", ...) do
    local value = select(i, ...)
    if value ~= nil then values[#values + 1] = tostring(value) end
  end
  local message = table.concat(values, " ")
  if message == "" then return end
  local lower = message:lower()
  local good = not (lower:find("error",1,true) or lower:find("failed",1,true)
    or lower:find("failure",1,true) or lower:find("invalid",1,true)
    or lower:find("cannot",1,true) or lower:find("could not",1,true)
    or lower:find("not found",1,true) or lower:find("required",1,true)
    or lower:find("unavailable",1,true) or lower:find("rejected",1,true))
  ui.message(message, good)
end
local CONFIG = "minebank_vending.json"
local JOURNAL = "minebank_vending_journal.json"
local function save(path,value)
  local tmp,backup=path..".tmp",path..".bak"
  local h=fs.open(tmp,"w"); if not h then return false end
  local ok=pcall(function() h.write(textutils.serializeJSON(value)); h.close() end)
  if not ok then pcall(function() h.close() end); fs.delete(tmp); return false end
  if fs.exists(backup) then fs.delete(backup) end
  if fs.exists(path) then
    local moved=pcall(function() fs.move(path,backup) end)
    if not moved then fs.delete(tmp); return false end
  end
  local moved=pcall(function() fs.move(tmp,path) end)
  if not moved then
    if fs.exists(backup) and not fs.exists(path) then pcall(function() fs.move(backup,path) end) end
    return false
  end
  if fs.exists(backup) then fs.delete(backup) end
  return true
end
local function load(path)
  if not fs.exists(path) and fs.exists(path..".bak") then pcall(function() fs.move(path..".bak",path) end) end
  if not fs.exists(path) then
    if fs.exists(path..".tmp") or fs.exists(path..".bak") then return {state="CORRUPT"} end
    return nil
  end
  local h=fs.open(path,"r"); if not h then return {state="CORRUPT"} end
  local raw=h.readAll(); h.close()
  local ok,v=pcall(textutils.unserializeJSON,raw)
  if ok and type(v)=="table" then return v end
  return {state="CORRUPT"}
end
local function stockCount(inv,item)
  local total=0
  for slot,detail in pairs(inv.list()) do
    local full=inv.getItemDetail(slot)
    if full and full.name==item then total=total+full.count end
  end
  return total
end
local function canFit(inv,item,count)
  -- Conservative for arbitrary catalogue items: an empty slot is counted as
  -- one item because non-stackable items may have maxCount=1.
  local left=count
  for slot=1,inv.size() do
    local detail=inv.getItemDetail(slot)
    if not detail then left=left-1
    elseif detail.name==item then
      left=left-math.max(0,(detail.maxCount or 64)-detail.count)
    end
    if left<=0 then return true end
  end
  return false
end
local function dispense(cfg, order)
  local stock=ui.inventory(cfg.stock_inventory)
  local output=ui.inventory(cfg.output_inventory)
  if not stock or not output then return false,"Configured stock/output inventory is unavailable." end
  if stockCount(stock,order.item)<order.count then return false,"Stock changed after payment. Keep the order journal and request operator reconciliation." end
  if not canFit(output,order.item,order.count) then return false,"Output inventory has insufficient space. The order is paid; clear output and reconcile." end
  order.state="DISPENSE_STARTED"
  if not save(JOURNAL,order) then return false,"Could not persist dispense lock; no items moved." end
  local remaining=order.count
  for slot,detail in pairs(stock.list()) do
    local full=stock.getItemDetail(slot)
    if full and full.name==order.item and remaining>0 then
      local moved=stock.pushItems(cfg.output_inventory,slot,math.min(remaining,full.count))
      remaining=remaining-moved
      if moved<=0 then return false,"Dispense stopped early. Operator reconciliation required." end
    end
  end
  if remaining>0 then return false,"Partial dispense detected. Operator reconciliation required." end
  order.state="COMPLETE"; order.completed_at=os.epoch("utc")
  if not save(JOURNAL,order) then return false,"Items moved but completion journal could not be saved. Lock the machine and reconcile." end
  return true
end

local function configureMachine(cfg)
  ui.useTarget("terminal")
  cfg.display_name = cfg.display_name or "MINEBANK VENDING"
  if cfg.accept_balance == nil then cfg.accept_balance = true end
  if cfg.accept_cashline == nil then cfg.accept_cashline = true end
  while true do
    ui.clear(); ui.header("VENDING SETTINGS","Operator controls - physical computer only")
    local options = {"Change machine display name","Edit product catalogue","Reassign stock/output inventories","Change HTTPS API address","Rotate merchant token","Toggle accepted payment methods","Return to customer kiosk"}
    local choice = ui.choose("VENDING SETTINGS", options,"Settings are local to this machine. Paid orders remain protected by the recovery journal.")
    if not choice or choice == 7 then
      if not save(CONFIG,cfg) then ui.message("Could not save settings. Check local storage.",false); ui.pause() end
      return cfg
    elseif choice == 1 then
      local name=ui.prompt("Machine display name",cfg.display_name)
      if name and name:match("%S") then cfg.display_name=name:sub(1,80) end
    elseif choice == 2 then
      local catalog={}
      for i=1,12 do
        local previous=(cfg.catalog or {})[i]
        local item=ui.prompt("Product "..i.." item ID (blank to finish)",previous and previous.item or "")
        if not item or item=="" then break end
        local price=tonumber(ui.prompt("Price in Emeralds",tostring(previous and previous.price or 1)))
        local count=tonumber(ui.prompt("Quantity dispensed",tostring(previous and previous.count or 1)))
        if not item:match("^[%w_%-]+:[%w_/%-]+$") or not price or price%1~=0 or price<1 or price>1000000 or not count or count%1~=0 or count<1 or count>64 then
          ui.message("Invalid item, price or quantity. That product was skipped.",false)
        else catalog[#catalog+1]={item=item,price=price,count=count} end
      end
      if #catalog>0 then cfg.catalog=catalog else ui.message("Catalogue unchanged because no valid products were entered.",false) end
    elseif choice == 3 then
      local inventories=ui.findInventory()
      if #inventories<2 then ui.message("At least two usable inventories are required.",false)
      else
        local s=ui.choose("STOCK INVENTORY",inventories,"Choose where products are stored.")
        if s and inventories[s] then
          local outputs={}
          for i,name in ipairs(inventories) do if i~=s then outputs[#outputs+1]=name end end
          local o=ui.choose("OUTPUT INVENTORY",outputs,"Choose a different inventory for deliveries.")
          if o and outputs[o] then cfg.stock_inventory=inventories[s]; cfg.output_inventory=outputs[o] end
        end
      end
    elseif choice == 4 then
      local api=ui.prompt("HTTPS API base",cfg.api_base or ui.API_BASE)
      if api and api:match("^https://") then cfg.api_base=api:gsub("/+$","") else ui.message("API address must start with https://. Address unchanged.",false) end
    elseif choice == 5 then
      local token=ui.secret("New merchant token (mbmerchant_...): ")
      if token and token:match("^mbmerchant_") and #token>=40 then cfg.merchant_token=token else ui.message("Token format is invalid. Existing token unchanged.",false) end
    elseif choice == 6 then
      local methods={(cfg.accept_balance and "[ON] " or "[OFF] ").."MineBank Balance",(cfg.accept_cashline and "[ON] " or "[OFF] ").."Standard CashLine","Back"}
      local method=ui.choose("PAYMENT METHODS",methods,"At least one method must remain enabled.")
      if method==1 then
        if cfg.accept_balance and not cfg.accept_cashline then ui.message("At least one payment method must remain enabled.",false) else cfg.accept_balance=not cfg.accept_balance end
      elseif method==2 then
        if cfg.accept_cashline and not cfg.accept_balance then ui.message("At least one payment method must remain enabled.",false) else cfg.accept_cashline=not cfg.accept_cashline end
      end
    end
    if not save(CONFIG,cfg) then ui.message("Settings could not be saved. Do not restart until the configuration is checked.",false); ui.pause() end
  end
end

local cfg=load(CONFIG)
if not cfg then
  ui.clear(); ui.header("VENDING SETUP","Local configuration wizard")
  print("Use a dedicated merchant credential on this locked-down machine.")
  cfg={}
  cfg.api_base=ui.prompt("HTTPS API base",ui.API_BASE)
  if not cfg.api_base or not cfg.api_base:match("^https://") then ui.message("HTTPS is required. Setup was not saved.",false); ui.pause(); return end
  ui.API_BASE=cfg.api_base:gsub("/+$","")
  cfg.merchant_token=ui.secret("Merchant token (mbmerchant_...): ")
  if not cfg.merchant_token or not cfg.merchant_token:match("^mbmerchant_") or #cfg.merchant_token<40 then ui.message("Invalid merchant token format.",false); ui.pause(); return end
  local inventories=ui.findInventory()
  if #inventories<2 then ui.message("Connect separate stock and output inventories.",false); ui.pause(); return end
  ui.message("Discovered "..#inventories.." usable inventories. Assign the stock and output roles.",true)
  local a=ui.choose("STOCK INVENTORY",inventories,"Which inventory stores merchandise for sale?")
  if not a or not inventories[a] then ui.message("Stock inventory selection cancelled.",false); ui.pause(); return end
  local b=ui.choose("CUSTOMER OUTPUT INVENTORY",inventories,"Choose a different inventory that delivers purchased items.")
  if not b or not inventories[b] or a==b then ui.message("Choose two different inventory peripherals.",false); ui.pause(); return end
  cfg.stock_inventory=inventories[a]; cfg.output_inventory=inventories[b]
  cfg.display_name="MINEBANK VENDING"; cfg.accept_balance=true; cfg.accept_cashline=true
  cfg.catalog={}
  print("Add up to 12 products. Use exact Minecraft item IDs, e.g. minecraft:apple.")
  for i=1,12 do
    local item=ui.prompt("Item ID "..i.." (blank to finish)")
    if not item or item=="" then break end
    local price=tonumber(ui.prompt("Price in Emeralds for "..item))
    local count=tonumber(ui.prompt("Items dispensed per purchase", "1"))
    if not item:match("^[%w_%-]+:[%w_/%-]+$") or not price or price%1~=0 or price<1 or price>1000000 or not count or count%1~=0 or count<1 or count>64 then
      ui.message("Invalid item, price or quantity. Product skipped.",false)
    else
      cfg.catalog[#cfg.catalog+1]={item=item,price=price,count=count}
    end
  end
  if #cfg.catalog==0 then ui.message("At least one valid product is required.",false); ui.pause(); return end
  if not save(CONFIG,cfg) then ui.message("Could not save local configuration.",false); ui.pause(); return end
end
ui.API_BASE=(cfg.api_base or ui.API_BASE):gsub("/+$","")
if not cfg.merchant_token or not cfg.merchant_token:match("^mbmerchant_") then ui.message("Merchant token is not configured.",false); ui.pause(); return end
ui.useTarget(ui.hasMonitor() and "monitor" or "terminal")
local old=load(JOURNAL)
if old and old.state=="DISPENSE_STARTED" then
  ui.useTarget("terminal")
  ui.clear(); ui.header("RECONCILIATION REQUIRED")
  print("Order "..tostring(old.checkout_id).." may have been partly dispensed.")
  print("The machine will not dispense it again automatically.")
  print("Inspect stock/output and ledger, then resolve manually.")
  ui.pause(); return
elseif old and old.state=="PAID" then
  ui.useTarget("terminal")
  ui.clear(); ui.header("RECOVER PAID ORDER")
  print("Order "..tostring(old.checkout_id).." is paid and not yet marked as dispensed.")
  print("Product: "..old.item.." x"..old.count)
  local phrase=ui.prompt("Type DISPENSE once to continue")
  if phrase~="DISPENSE" then return end
  local ok,err=dispense(cfg,old)
  ui.message(ok and "Order fulfilled." or err,ok)
  ui.pause(); return
elseif old and old.state~="COMPLETE" and old.state~="CANCELLED" then
  ui.useTarget("terminal")
  ui.clear(); ui.header("ORDER RECOVERY REQUIRED")
  print("An unfinished order exists: "..tostring(old.checkout_id or old.key))
  print("State: "..tostring(old.state))
  print("The machine will not create a new checkout or dispense again automatically.")
  print("Check the original MineBank checkout status and reconcile before continuing.")
  ui.pause(); return
end

while true do
  ui.useTarget(ui.hasMonitor() and "monitor" or "terminal")
  local opts={}
  for _,p in ipairs(cfg.catalog or {}) do opts[#opts+1]=p.item.." x"..p.count.." - "..p.price.." Emeralds" end
  -- The customer touchscreen never offers Exit. F12 on the physical computer opens settings.
  if not ui.hasMonitor() then opts[#opts+1]="Exit" end
  local choice=ui.choose(cfg.display_name or "VENDING MACHINE",opts,
    ui.hasMonitor() and "Touch a product to begin checkout." or "Choose a product or Exit.")
  if ui.consumeAdminRequest and ui.consumeAdminRequest() then
    cfg=configureMachine(cfg)
  elseif not choice then
    if not ui.hasMonitor() then break end
  elseif choice>#(cfg.catalog or {}) then
    if not ui.hasMonitor() then break end
  else
  local product=cfg.catalog[choice]
  local stock=ui.inventory(cfg.stock_inventory)
  local output=ui.inventory(cfg.output_inventory)
  if not stock or not output then ui.message("Stock/output inventory unavailable.",false); ui.pause()
  elseif stockCount(stock,product.item)<product.count then ui.message("OUT OF STOCK: "..product.item,false); ui.pause()
  elseif not canFit(output,product.item,product.count) then ui.message("Output inventory is full. No payment taken.",false); ui.pause()
  else
    local confirm=ui.choose("CONFIRM PURCHASE",{"Buy for "..product.price.." Emeralds","Cancel"},product.item.." × "..product.count)
    if confirm==1 then
      local order={version=1,key=ui.key("vending"),item=product.item,count=product.count,amount=product.price,state="CHECKOUT_PENDING"}
      if not save(JOURNAL,order) then ui.message("Could not persist order; no checkout created.",false); ui.pause()
      else
        local checkout,err,checkoutHttp=ui.api("/merchant/checkouts","POST",cfg.merchant_token,
          {amount=product.price,description="Vending: "..product.item.." x"..product.count,reference=order.key},order.key)
        if not checkout then
          order.state="CHECKOUT_REVIEW"
          order.checkout_http_status=checkoutHttp
          order.checkout_error=tostring(err or "Unknown checkout creation result")
          save(JOURNAL,order)
          ui.message("Checkout creation was not confirmed (HTTP "..tostring(checkoutHttp or "unknown").."): "..tostring(err),false)
          ui.message("The request will not be resent automatically. The register is locked so an uncertain checkout cannot be duplicated.",false)
          ui.pause()
          return
        else
          order.checkout_id=tostring(checkout.checkout_id); order.state="AWAITING_PAYMENT"
          if not save(JOURNAL,order) then
            ui.message("MineBank returned a checkout ID but it could not be persisted. Do not open another sale; operator reconciliation is required.",false)
            ui.pause()
            return
          else
             local card
             while not card do
               local ready=ui.choose("CUSTOMER CARD",{"I have inserted my MineBank card","Cancel purchase"},
                 "Insert the MineBank floppy card into the drive. It is only read for this purchase.")
               if ready~=1 then break end
               local cardErr
               card,cardErr=ui.card()
               if not card then ui.message(cardErr or "Card not found in the drive.",false); ui.pause() end
             end
             if not card then
               order.state="CANCELLED"; save(JOURNAL,order)
               ui.message("Purchase cancelled before authorisation. No payment was sent.",true)
               ui.pause()
             else
               local pin=ui.secret("Customer Wallet PIN: ")
               if not pin or not pin:match("^%d%d%d%d%d%d$") then
                 order.state="CANCELLED"; save(JOURNAL,order)
                 ui.message("A valid six-digit Wallet PIN is required. No payment was sent.",false)
                 ui.pause()
               else
                 local sourceOptions,sourceKinds={},{}
                 if cfg.accept_balance ~= false then sourceOptions[#sourceOptions+1]="MineBank Balance"; sourceKinds[#sourceKinds+1]="BALANCE" end
                 if cfg.accept_cashline ~= false then sourceOptions[#sourceOptions+1]="Standard CashLine"; sourceKinds[#sourceKinds+1]="CASHLINE" end
                 sourceOptions[#sourceOptions+1]="Cancel"
                 local source=ui.choose("PAYMENT SOURCE",sourceOptions,
                   "Purchase: "..product.price.." Emeralds - "..product.item.." x"..product.count)
                 local funding=(source and sourceKinds[source]) or nil
                 if funding then
                   ui.clear(); ui.header("AUTHORISE PURCHASE","Payment source: "..(funding=="BALANCE" and "MineBank Balance" or "Standard CashLine"))
                   local final=ui.choose("FINAL AUTHORISATION",{"Authorise "..product.price.." Emeralds","Cancel"},
                     "Merchant purchase: "..product.item.." × "..product.count)
                   if final==1 then
                     local payKey="checkout-"..order.checkout_id.."-"..funding
                     local paid,payErr,payStatus=ui.api("/payments","POST",card.credential,
                       {checkout_id=order.checkout_id,wallet_pin=pin,funding_source=funding},payKey)
                     if not paid then
                       ui.message("Payment rejected / not confirmed (HTTP "..tostring(payStatus or "unknown").."): "..tostring(payErr),false)
                       if payStatus and (payStatus==400 or payStatus==401 or payStatus==403 or payStatus==404 or payStatus==422) then
                         -- A definite endpoint rejection is not an ambiguous transaction.
                         -- Never resend the POST; the rejected transaction was not committed.
                         order.state="CANCELLED"
                         order.payment_http_status=payStatus
                         order.payment_error=tostring(payErr or "Payment was rejected")
                         save(JOURNAL,order)
                         ui.message("MineBank rejected this payment. No balance debit or merchant credit was committed. Start a new purchase with another valid payment source.",false)
                       else
                         -- For 409/network/5xx/timeout, query the existing checkout only.
                         -- Never retry the payment POST automatically.
                         local status,statusErr=ui.api("/merchant/checkouts/"..order.checkout_id,"GET",cfg.merchant_token)
                         if status and status.status=="PAID" then
                           order.state="PAID"; order.transaction_id=status.transaction_id
                           if save(JOURNAL,order) then ui.message("MineBank confirms the original checkout is paid. The item can be reconciled; no payment was resent.",true)
                           else ui.message("Payment is confirmed but local journal failed. Lock machine and reconcile.",false) end
                         else
                           order.state="PAYMENT_REVIEW"
                           order.payment_http_status=payStatus
                           order.payment_error=tostring(payErr or "Unknown payment error")
                           order.checkout_status=status and status.status or "UNKNOWN"
                           order.status_error=statusErr and tostring(statusErr) or nil
                           save(JOURNAL,order)
                           ui.message("No payment retry was sent. Order is locked for reconciliation before a new sale.",false)
                         end
                       end
                       ui.pause()
                     elseif paid.status~="COMPLETED" then
                       order.state="PAYMENT_REVIEW"
                       order.payment_error="Unexpected payment status: "..tostring(paid.status)
                       save(JOURNAL,order)
                       ui.message("Payment status was not COMPLETED. No items dispensed; operator reconciliation is required.",false)
                       ui.pause()
                     else
                       order.state="PAID"; order.transaction_id=paid.transaction_id
                       if not save(JOURNAL,order) then ui.message("Payment completed but journal failed. Do not dispense; operator reconciliation required.",false); ui.pause()
                       else
                         local ok,dispErr=dispense(cfg,order)
                         ui.message(ok and ("PURCHASE COMPLETE — "..product.item.." x"..product.count) or dispErr,ok)
                         ui.pause()
                       end
                     end
                     if order.state~="COMPLETE" and order.state~="CANCELLED" then return end
                   else
                     order.state="CANCELLED"; save(JOURNAL,order)
                     ui.message("Purchase cancelled. No payment was sent.",true); ui.pause()
                   end
                 else
                   order.state="CANCELLED"; save(JOURNAL,order)
                   ui.message("Purchase cancelled. No payment was sent.",true); ui.pause()
                 end
                 pin=nil
               end
             end
              end
            end
          end
        end
      end
    end
  end
  ui.clear(); ui.header("VENDING MACHINE","Thank you for using MineBank.")
