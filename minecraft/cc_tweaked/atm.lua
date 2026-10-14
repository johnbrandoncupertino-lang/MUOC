-- MineBank ATM for CC:Tweaked computers.
-- Uses separate deposit, secure stock/escrow and output inventories.
-- Never reports a financial operation as successful without authoritative API confirmation.

local ui=dofile("minebank_ui.lua")
local CONFIG="minebank_atm.json"
local JOURNAL="minebank_atm_journal.json"
local MAX_WITHDRAWAL=4096
local MAX_TRANSFER=1000

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
  if not fs.exists(path) and fs.exists(path..".bak") then
    pcall(function() fs.move(path..".bak",path) end)
  end
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
local function countItems(inv,item)
  local n=0
  for slot,detail in pairs(inv.list()) do
    if detail.name==item then n=n+detail.count end
  end
  return n
end
local function countEmeralds(inv) return countItems(inv,"minecraft:emerald") end
local function canFit(inv,item,count)
  local remaining=count
  for slot=1,inv.size() do
    local d=inv.getItemDetail(slot)
    if not d then remaining=remaining-64
    elseif d.name==item then remaining=remaining-math.max(0,64-d.count) end
    if remaining<=0 then return true end
  end
  return false
end
local function moveEmeralds(source,destination)
  local moved=0
  for slot,detail in pairs(source.list()) do
    if detail.name=="minecraft:emerald" then
      local n=source.pushItems(destination,slot,detail.count)
      moved=moved+n
    end
  end
  return moved
end
local function draw(cfg)
  ui.clear(); ui.header("ATM","Secure terminal • physical emeralds = MineBank Emeralds")
  print("1) Withdraw emeralds")
  print("2) Deposit emeralds")
  print("3) Transfer to another account (max 1,000)")
  print("4) Recent transactions")
  print("5) Revoke a card")
  print("6) Setup / diagnostics")
  print("7) Exit")
  write("> "); return tonumber(read())
end
local function getCard()
  local card,err=ui.card()
  if not card then ui.message(err,false); ui.pause(); return nil end
  return card
end
local function pin()
  local p=ui.secret("Wallet PIN (6 digits): ")
  if not p or not p:match("^%d%d%d%d%d%d$") then ui.message("PIN must contain six digits.",false); return nil end
  return p
end
local function ensureConfig()
  local cfg=load(CONFIG)
  if cfg and cfg.atm_token and cfg.api_base then ui.API_BASE=(cfg.api_base or ui.API_BASE):gsub("/+$",""); return cfg end
  ui.clear(); ui.header("ATM SETUP","Guided peripheral configuration")
  local base=ui.prompt("HTTPS MineBank API base",ui.API_BASE)
  if not base:match("^https://") then return nil,"API base must use HTTPS." end
  ui.API_BASE=base:gsub("/+$","")
  local atmToken=ui.secret("Trusted ATM token from MineBank operator configuration: ")
  if not atmToken or #atmToken<32 then return nil,"A trusted ATM token of at least 32 characters is required." end
  local names=ui.findInventory()
  if #names<3 then return nil,"Connect deposit, secure stock/escrow and output inventories, then rerun setup." end
  print("Detected inventories:")
  for i,n in ipairs(names) do print(i..") "..n) end
  local d=tonumber(ui.prompt("Deposit inventory number"))
  local s=tonumber(ui.prompt("Secure stock/escrow inventory number"))
  local o=tonumber(ui.prompt("Output inventory number"))
  if not names[d] or not names[s] or not names[o] or d==s or d==o or s==o then return nil,"Select three different inventory numbers." end
  local cfg={api_base=ui.API_BASE,atm_token=atmToken,deposit_inventory=names[d],stock_inventory=names[s],output_inventory=names[o]}
  if not save(CONFIG,cfg) then return nil,"Could not save ATM configuration." end
  return cfg
end
local cfg,cfgErr=ensureConfig()
if not cfg then ui.message(cfgErr,false); ui.pause(); return end
if not http then ui.message("Enable the CC:Tweaked HTTP API and allow the MineBank HTTPS host.",false); ui.pause(); return end
local depositInv=ui.inventory(cfg.deposit_inventory)
local stockInv=ui.inventory(cfg.stock_inventory)
local outputInv=ui.inventory(cfg.output_inventory)
if not depositInv or not stockInv or not outputInv then ui.message("Configured inventory missing. Run setup again.",false); ui.pause(); return end

local function runWithdrawal()
  local card=getCard(); if not card then return end
  local p=pin(); if not p then return end
  local old=load(JOURNAL)
  local order
  if old and old.state~="COMPLETE" then
    if old.kind~="WITHDRAWAL" or (old.state~="REQUEST_PENDING" and old.state~="AUTHORIZED") then
      ui.message("An unfinished ATM operation exists. Reconcile it before starting another.",false); ui.pause(); return
    end
    if tostring(old.card_id or "")~=tostring(card.card_id or "") then ui.message("This unfinished withdrawal belongs to a different floppy card. Reinsert the original card.",false); ui.pause(); return end
    order=old
  else
    local amount=tonumber(ui.prompt("Whole Emeralds to withdraw (1-"..MAX_WITHDRAWAL..")"))
    if not amount or amount%1~=0 or amount<1 or amount>MAX_WITHDRAWAL then ui.message("Invalid withdrawal amount.",false); ui.pause(); return end
    if countEmeralds(stockInv)<amount then ui.message("Secure stock does not contain enough emeralds.",false); ui.pause(); return end
    if not canFit(outputInv,"minecraft:emerald",amount) then ui.message("Output inventory lacks space. No debit requested.",false); ui.pause(); return end
    order={version=1,key=ui.key("withdraw"),amount=amount,state="REQUEST_PENDING",kind="WITHDRAWAL",card_id=card.card_id}
    if not save(JOURNAL,order) then ui.message("Could not persist journal. No request sent.",false); ui.pause(); return end
  end
  if countEmeralds(stockInv)<order.amount then ui.message("Secure stock is short. The saved withdrawal has not been re-requested.",false); ui.pause(); return end
  if not canFit(outputInv,"minecraft:emerald",order.amount) then ui.message("Output inventory lacks space. No new debit requested.",false); ui.pause(); return end
  local result
  if order.state=="REQUEST_PENDING" then
    local err
    result,err=ui.api("/withdrawals","POST",card.credential,{amount=order.amount,wallet_pin=p},order.key,{["X-MineBank-ATM-Token"]=cfg.atm_token})
    if not result then ui.message("Withdrawal not confirmed: "..tostring(err)..". The same key is saved; check status before starting another.",false); ui.pause(); return end
    order.state="AUTHORIZED"; order.transaction_id=result.transaction_id
    if not save(JOURNAL,order) then ui.message("MineBank debited the account but the local ticket could not be saved. Do not dispense.",false); ui.pause(); return end
  else
    result={transaction_id=order.transaction_id,amount=order.amount}
  end
  order.state="DISPENSE_STARTED"
  if not save(JOURNAL,order) then ui.message("Could not lock the dispense journal. No items moved; operator reconciliation required.",false); ui.pause(); return end
  local remaining=order.amount
  for slot,detail in pairs(stockInv.list()) do
    if detail.name=="minecraft:emerald" and remaining>0 then
      local moved=stockInv.pushItems(cfg.output_inventory,slot,math.min(remaining,detail.count))
      remaining=remaining-moved
      if moved<=0 then break end
    end
  end
  if remaining>0 then ui.message("Partial dispense detected. Do not retry. Operator reconciliation required.",false); ui.pause(); return end
  order.state="COMPLETE"; order.completed_at=os.epoch("utc")
  if not save(JOURNAL,order) then ui.message("Items moved but journal completion failed. Lock ATM and reconcile.",false); ui.pause(); return end
  ui.message("WITHDRAWAL COMPLETE — "..order.amount.." Emeralds",true)
  print("Transaction: "..tostring(result.transaction_id)); ui.pause()
end

local function runDeposit()
  local card=getCard(); if not card then return end
  local p=pin(); if not p then return end
  local old=load(JOURNAL)
  local order
  if old and old.state~="COMPLETE" then
    if old.kind~="DEPOSIT" or (old.state~="REQUEST_PENDING" and old.state~="ESCROW_PENDING") then
      ui.message("An unfinished ATM operation exists. Reconcile it before starting another.",false); ui.pause(); return
    end
    if tostring(old.card_id or "")~=tostring(card.card_id or "") then ui.message("This unfinished deposit belongs to a different floppy card. Reinsert the original card.",false); ui.pause(); return end
    order=old
  else
    local amount=countEmeralds(depositInv)
    if amount<1 then ui.message("Place emeralds in the deposit inventory first.",false); ui.pause(); return end
    if amount>4096 then ui.message("Deposit is limited to 4,096 Emeralds per operation.",false); ui.pause(); return end
    if not canFit(stockInv,"minecraft:emerald",amount) then ui.message("Secure stock inventory lacks space. No items moved.",false); ui.pause(); return end
    order={version=1,key=ui.key("deposit"),amount=amount,state="ESCROW_PENDING",kind="DEPOSIT",card_id=card.card_id}
    if not save(JOURNAL,order) then ui.message("Could not persist deposit journal; items left untouched.",false); ui.pause(); return end
  end
  if order.state=="ESCROW_PENDING" then
    local moved=moveEmeralds(depositInv,cfg.stock_inventory)
    if moved~=order.amount then ui.message("Only "..moved.." of "..order.amount.." Emeralds entered escrow. Stop and reconcile.",false); ui.pause(); return end
    order.state="REQUEST_PENDING"
    if not save(JOURNAL,order) then ui.message("Emeralds are secured in escrow but journal failed. Do not move them.",false); ui.pause(); return end
  end
  local result,err=ui.api("/deposits","POST",card.credential,{amount=order.amount,wallet_pin=p},order.key,{["X-MineBank-ATM-Token"]=cfg.atm_token})
  if not result then
    ui.message("Deposit not confirmed: "..tostring(err)..". Emeralds remain in secure stock; re-run to retry the same operation.",false)
    ui.pause(); return
  end
  order.state="COMPLETE"; order.transaction_id=result.transaction_id; order.completed_at=os.epoch("utc")
  if not save(JOURNAL,order) then ui.message("MineBank confirmed the deposit but journal update failed. Preserve the operation key and reconcile.",false); ui.pause(); return end
  ui.message("DEPOSIT COMPLETE — "..order.amount.." Emeralds",true)
  print("Transaction: "..tostring(result.transaction_id)); ui.pause()
end

local function runTransfer()
  local card=getCard(); if not card then return end
  local p=pin(); if not p then return end
  local old=load(JOURNAL)
  local order
  if old and old.state~="COMPLETE" then
    if old.kind~="TRANSFER" or old.state~="REQUEST_PENDING" then
      ui.message("An unfinished ATM operation exists. Reconcile it before starting another.",false); ui.pause(); return
    end
    if tostring(old.card_id or "")~=tostring(card.card_id or "") then ui.message("This unfinished transfer belongs to a different floppy card. Reinsert the original card.",false); ui.pause(); return end
    order=old
  else
    local recipient=ui.prompt("Recipient account number")
    local amount=tonumber(ui.prompt("Amount in Emeralds (1-1000)"))
    if not amount or amount%1~=0 or amount<1 or amount>MAX_TRANSFER then ui.message("Transfers are limited to 1,000 Emeralds. Use the portal for larger transfers.",false); ui.pause(); return end
    ui.clear(); ui.header("CONFIRM TRANSFER")
    print("Recipient: "..recipient); print("Amount: "..amount.." Emeralds")
    write("Type TRANSFER to confirm: ")
    if read()~="TRANSFER" then ui.message("Cancelled. No transfer submitted.",true); ui.pause(); return end
    order={version=1,key=ui.key("transfer"),amount=amount,recipient=recipient,state="REQUEST_PENDING",kind="TRANSFER",card_id=card.card_id}
    if not save(JOURNAL,order) then ui.message("Could not save transfer journal; no request sent.",false); ui.pause(); return end
  end
  local result,err=ui.api("/transfers","POST",card.credential,
    {recipient_account_number=order.recipient,amount=order.amount,wallet_pin=p},order.key)
  if not result then
    ui.message("Transfer not confirmed: "..tostring(err)..". The original key is saved; retry only with the same card.",false)
    ui.pause(); return
  end
  order.state="COMPLETE"; order.transaction_id=result.transaction_id; order.completed_at=os.epoch("utc")
  if not save(JOURNAL,order) then ui.message("MineBank confirmed the transfer but the journal update failed. Preserve the key and reconcile.",false); ui.pause(); return end
  ui.message("TRANSFER COMPLETE — "..order.amount.." Emeralds",true)
  print("Transaction: "..tostring(result.transaction_id)); print("Fee: "..tostring(result.fee or 0)); ui.pause()
end

local function runHistory()
  local card=getCard(); if not card then return end
  local result,err=ui.api("/history","GET",card.credential)
  if not result then ui.message("History unavailable: "..tostring(err),false)
  else
    ui.clear(); ui.header("RECENT TRANSACTIONS")
    for _,tx in ipairs(result.history or {}) do
      print(tostring(tx.type).." | "..tostring(tx.amount).." | "..tostring(tx.status))
      print("  "..tostring(tx.transaction_id).." "..tostring(tx.description):sub(1,30))
    end
  end
  ui.pause()
end

local function runRevoke()
  local card=getCard(); if not card then return end
  ui.clear(); ui.header("REVOKE CARD","This permanently disables the inserted floppy card.")
  print("Card ID: "..tostring(card.card_id or "unknown"))
  print("You must issue a new card to use MineBank again.")
  write("Type REVOKE to confirm: ")
  if read()~="REVOKE" then ui.message("Cancelled. The card remains active.",true); ui.pause(); return end
  local p=pin(); if not p then return end
  local result,err=ui.api("/cards/revoke-self","POST",card.credential,{wallet_pin=p,confirm=true})
  if not result then ui.message("Revocation not confirmed: "..tostring(err),false)
  else ui.message("CARD REVOKED. This floppy can no longer be used.",true) end
  ui.pause()
end

while true do
  local choice=draw(cfg)
  if choice==1 then runWithdrawal()
  elseif choice==2 then runDeposit()
  elseif choice==3 then runTransfer()
  elseif choice==4 then runHistory()
  elseif choice==5 then runRevoke()
  elseif choice==6 then
    ui.clear(); ui.header("DIAGNOSTICS")
    print("API: "..ui.API_BASE)
    print("Deposit inventory: "..cfg.deposit_inventory)
    print("Secure stock/escrow: "..cfg.stock_inventory)
    print("Output inventory: "..cfg.output_inventory)
    print("Deposit emeralds: "..countEmeralds(depositInv))
    print("Stock emeralds: "..countEmeralds(stockInv))
    print("Output emeralds: "..countEmeralds(outputInv))
    print("HTTP enabled: "..tostring(http~=nil))
    ui.pause()
  else break end
end
ui.clear(); ui.header("MINEBANK ATM","Thank you for banking with MineBank.")
