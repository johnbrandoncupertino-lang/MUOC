-- MineBank automated vending terminal for CC:Tweaked.
-- Requires a trusted merchant token, a stock inventory and a separate output inventory.
-- A paid order is journalled before dispensing; interrupted physical delivery is never replayed automatically.

local ui = dofile("minebank_ui.lua")
local CONFIG = "minebank_vending.json"
local JOURNAL = "minebank_vending_journal.json"
local function save(path, value)
  local tmp = path..".tmp"
  local h = fs.open(tmp,"w"); if not h then return false end
  local ok = pcall(function() h.write(textutils.serializeJSON(value)); h.close() end)
  if not ok then pcall(function() h.close() end); fs.delete(tmp); return false end
  if fs.exists(path) then fs.delete(path) end
  local moved = pcall(function() fs.move(tmp,path) end)
  return moved
end
local function load(path)
  if not fs.exists(path) then return nil end
  local h=fs.open(path,"r"); if not h then return nil end
  local raw=h.readAll(); h.close()
  local ok,v=pcall(textutils.unserializeJSON,raw)
  if ok and type(v)=="table" then return v end
  return nil
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
  local left=count
  local list=inv.list()
  for slot=1,inv.size() do
    local detail=inv.getItemDetail(slot)
    if not detail then left=left-64
    elseif detail.name==item then left=left+0-math.min(left, math.max(0,64-detail.count)) end
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

local cfg=load(CONFIG)
if not cfg then
  ui.clear(); ui.header("VENDING SETUP","Local configuration wizard")
  print("Use a dedicated merchant credential on this locked-down machine.")
  cfg={}
  cfg.api_base=ui.prompt("HTTPS API base",ui.API_BASE)
  if not cfg.api_base:match("^https://") then ui.message("HTTPS is required.",false); ui.pause(); return end
  ui.API_BASE=cfg.api_base:gsub("/+$","")
  cfg.merchant_token=ui.secret("Merchant token (mbmerchant_...): ")
  if not cfg.merchant_token:match("^mbmerchant_") then ui.message("Invalid merchant token format.",false); ui.pause(); return end
  local inventories=ui.findInventory()
  if #inventories<2 then ui.message("Connect separate stock and output inventories.",false); ui.pause(); return end
  print("Inventories:")
  for i,name in ipairs(inventories) do print(i..") "..name) end
  local a=tonumber(ui.prompt("Stock inventory number"))
  local b=tonumber(ui.prompt("Output inventory number"))
  if not a or not b or a==b or not inventories[a] or not inventories[b] then ui.message("Invalid inventory selection.",false); ui.pause(); return end
  cfg.stock_inventory=inventories[a]; cfg.output_inventory=inventories[b]
  cfg.catalog={}
  print("Add up to 12 products. Use exact Minecraft item IDs, e.g. minecraft:apple.")
  for i=1,12 do
    local item=ui.prompt("Item ID "..i.." (blank to finish")
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
local old=load(JOURNAL)
if old and old.state=="DISPENSE_STARTED" then
  ui.clear(); ui.header("RECONCILIATION REQUIRED")
  print("Order "..tostring(old.checkout_id).." may have been partly dispensed.")
  print("The machine will not dispense it again automatically.")
  print("Inspect stock/output and ledger, then resolve manually.")
  ui.pause(); return
elseif old and old.state~="COMPLETE" then
  ui.clear(); ui.header("ORDER RECOVERY REQUIRED")
  print("An unfinished order exists: "..tostring(old.checkout_id or old.key))
  print("State: "..tostring(old.state))
  print("The machine will not create a new checkout or dispense again automatically.")
  print("Check the original MineBank checkout status and reconcile before continuing.")
  ui.pause(); return
elseif old and old.state=="PAID" then
  ui.clear(); ui.header("RECOVER PAID ORDER")
  print("Order "..tostring(old.checkout_id).." is paid and not yet marked as dispensed.")
  print("Product: "..old.item.." x"..old.count)
  write("Type DISPENSE to continue once: ")
  if read()~="DISPENSE" then return end
  local ok,err=dispense(cfg,old)
  ui.message(ok and "Order fulfilled." or err,ok)
  ui.pause(); return
end

while true do
  local opts={}
  for _,p in ipairs(cfg.catalog or {}) do opts[#opts+1]=p.item.." x"..p.count.." — "..p.price.." Emeralds" end
  opts[#opts+1]="Exit"
  local choice=ui.choose("VENDING MACHINE",opts)
  if not choice or choice>#(cfg.catalog or {}) then break end
  local product=cfg.catalog[choice]
  local stock=ui.inventory(cfg.stock_inventory)
  local output=ui.inventory(cfg.output_inventory)
  if not stock or not output then ui.message("Stock/output inventory unavailable.",false); ui.pause()
  elseif stockCount(stock,product.item)<product.count then ui.message("OUT OF STOCK: "..product.item,false); ui.pause()
  elseif not canFit(output,product.item,product.count) then ui.message("Output inventory is full. No payment taken.",false); ui.pause()
  else
    ui.clear(); ui.header("CONFIRM PURCHASE",product.item)
    print("Price: "..product.price.." Emeralds")
    print("Quantity: "..product.count)
    write("Type BUY to continue: ")
    if read()=="BUY" then
      local order={version=1,key=ui.key("vending"),item=product.item,count=product.count,amount=product.price,state="CHECKOUT_PENDING"}
      if not save(JOURNAL,order) then ui.message("Could not persist order; no checkout created.",false); ui.pause()
      else
        local checkout,err=ui.api("/merchant/checkouts","POST",cfg.merchant_token,
          {amount=product.price,description="Vending: "..product.item.." x"..product.count,reference=order.key},order.key)
        if not checkout then ui.message("Checkout not confirmed: "..tostring(err),false); ui.pause()
        else
          order.checkout_id=tostring(checkout.checkout_id); order.state="AWAITING_PAYMENT"
          if not save(JOURNAL,order) then ui.message("Checkout exists but journal failed. Do not create another checkout; operator reconciliation required.",false); ui.pause()
          else
            print("Insert the customer's MineBank floppy card.")
            local card,cardErr=ui.card()
            if not card then ui.message(cardErr,false); ui.pause()
            else
              local pin=ui.secret("Customer Wallet PIN: ")
              print("Confirming "..product.price.." Emeralds to "..tostring(checkout.merchant or "merchant").."...")
              local paid,payErr=ui.api("/payments","POST",card.credential,
                {checkout_id=order.checkout_id,wallet_pin=pin},"checkout-"..order.checkout_id)
              if not paid then
                ui.message("Payment not confirmed: "..tostring(payErr),false)
                local status,statusErr=ui.api("/merchant/checkouts/"..order.checkout_id,"GET",cfg.merchant_token)
                if status and status.status=="PAID" then
                  order.state="PAID"; order.transaction_id=status.transaction_id
                  if save(JOURNAL,order) then ui.message("MineBank confirms payment. Re-run and reconcile/fulfil the paid order.",true)
                  else ui.message("Payment is confirmed but local journal failed. Lock machine and reconcile.",false) end
                else
                  ui.message("Do not create another order. Check the original checkout status before retrying.",false)
                end
                ui.pause()
              elseif paid.status~="COMPLETED" then
                ui.message("Payment response was not COMPLETED. No items dispensed.",false); ui.pause()
              else
                order.state="PAID"; order.transaction_id=paid.transaction_id
                if not save(JOURNAL,order) then ui.message("Payment completed but journal failed. Do not dispense; operator reconciliation required.",false); ui.pause()
                else
                  local ok,dispErr=dispense(cfg,order)
                  ui.message(ok and ("PURCHASE COMPLETE — "..product.item.." x"..product.count) or dispErr,ok)
                  ui.pause()
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
