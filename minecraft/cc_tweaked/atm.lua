-- MineBank ATM for CC:Tweaked computers.
-- Uses separate deposit, secure stock/escrow and output inventories.
-- Never reports a financial operation as successful without authoritative API confirmation.

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
local CONFIG="minebank_atm.json"
local JOURNAL="minebank_atm_journal.json"
local TRANSFER_JOURNAL="minebank_atm_transfer_journal.json"
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
local function readJournalFile(file)
  if not fs.exists(file) then return nil,false end
  local h=fs.open(file,"r")
  if not h then return nil,false end
  local raw=h.readAll(); h.close()
  local ok,value=pcall(textutils.unserializeJSON,raw)
  if ok and type(value)=="table" then return value,true end
  return nil,false
end
local function load(path)
  -- Restore the last committed journal if a save was interrupted after moving
  -- the primary file to its backup.
  if not fs.exists(path) and fs.exists(path..".bak") then
    local restored=pcall(function() fs.move(path..".bak",path) end)
    if not restored or not fs.exists(path) then return {state="CORRUPT"} end
  end

  if not fs.exists(path) then
    -- A valid .tmp can be left behind if the computer stops during the very
    -- first journal save. That save happens before any API request or physical
    -- item movement, so recover its original idempotency key instead of
    -- inventing a new operation or permanently reporting a false pending one.
    if fs.exists(path..".tmp") and not fs.exists(path..".bak") then
      local pending,valid=readJournalFile(path..".tmp")
      -- load() also reads the ATM configuration. Recover its complete temp file
      -- too, otherwise a restart after successful pairing could enroll another ATM.
      if valid and path==CONFIG then
        local promoted=pcall(function() fs.move(path..".tmp",path) end)
        if promoted and fs.exists(path) then return pending end
        return {state="CORRUPT"}
      end
      local initialState=valid and type(pending.key)=="string"
        and type(pending.amount)=="number"
        and ((pending.state=="REQUEST_PENDING"
              and (pending.kind=="WITHDRAWAL" or pending.kind=="TRANSFER"))
          or (pending.state=="ESCROW_PENDING" and pending.kind=="DEPOSIT"))
      if initialState then
        local promoted=pcall(function() fs.move(path..".tmp",path) end)
        if promoted and fs.exists(path) then return pending end
        return {state="CORRUPT"}
      elseif not valid then
        -- With no primary or backup, an unreadable temp is an interrupted
        -- first save. The program cannot have sent its request because save()
        -- must succeed before it does so.
        local removed=pcall(function() fs.delete(path..".tmp") end)
        if removed and not fs.exists(path..".tmp") then return nil end
      end
      return {state="CORRUPT"}
    end
    if fs.exists(path..".bak") then return {state="CORRUPT"} end
    return nil
  end

  local value,valid=readJournalFile(path)
  if valid then return value end
  return {state="CORRUPT"}
end
local function reportDamagedJournal()
  ui.message("The local ATM journal is damaged. This does not prove that a bank operation is pending, but the ATM cannot safely infer the last operation's outcome.",false)
  ui.message("Money movement is paused until the journal and account history are checked. Do not delete journal backups if a withdrawal or deposit may have started.",false)
  ui.autoPause(3)
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
local function moveEmeraldAmount(source,destination,amount,destinationName)
  local moved=0
  for slot,detail in pairs(source.list()) do
    if detail.name=="minecraft:emerald" and moved<amount then
      local n=source.pushItems(destinationName,slot,math.min(detail.count,amount-moved))
      moved=moved+n
    end
  end
  return moved
end
local function cancelRejected(order,err,journalPath)
  order.state="CANCELLED"
  order.failure=tostring(err or "MineBank rejected the request.")
  order.cancelled_at=os.epoch("utc")
  return save(journalPath or JOURNAL,order)
end
local function returnInvalidItems(source,destination)
  local returned=0
  for slot,detail in pairs(source.list()) do
    if detail.name~="minecraft:emerald" then
      local moved=source.pushItems(destination,slot,detail.count)
      returned=returned+moved
    end
  end
  return returned
end
local function draw(cfg)
  local options
  local journal=load(JOURNAL)
  local transferJournal=load(TRANSFER_JOURNAL)
  local function unfinished(value)
    return value and value.state~="COMPLETE" and value.state~="CANCELLED"
  end
  local pending=unfinished(journal) or unfinished(transferJournal)
  if ui.hasMonitor() then
    -- Put recovery first whenever a journal is unfinished, including on tiny monitors.
    if pending then
      options = {"Resume pending operation","Check balance","More services"}
    else
      options = {"Check balance","Withdraw Emeralds","Deposit Emeralds","More services"}
    end
  else
    options = {
      "Check balance",
      "Withdraw Emeralds",
      "Deposit Emeralds",
      "Transfer to another account (max 1,000)",
      "Recent transactions",
      "Revoke floppy card",
      "Diagnostics",
      "Resume pending operation",
      "Exit",
    }
  end
  local choice=ui.choose("MINEBANK ATM", options,
    (cfg.atm_label or "THE NEW BANK").." • "..(pending and "Recovery required" or "Secure Emerald services"))
  return choice,pending
end
local function drawMore()
  return ui.choose("MORE SERVICES", {
    "Transfer to another account (max 1,000)",
    "Recent transactions",
    "Revoke floppy card",
    "Diagnostics",
    "Resume pending operation",
    "Back",
  }, "THE NEW BANK • Account and recovery services.")
end

local function getCard()
  local card,err=ui.card()
  if not card then ui.message(err,false); ui.autoPause(2.4); return nil end
  return card
end
local function pin()
  local p=ui.secret("Wallet PIN (6 digits): ")
  if not p or not p:match("^%d%d%d%d%d%d$") then ui.message("PIN must contain six digits.",false); return nil end
  return p
end
local function ensureConfig()
  local cfg=load(CONFIG)
  if cfg and cfg.pairing_version==1 and cfg.atm_token and cfg.api_base then
    ui.API_BASE=(cfg.api_base or ui.API_BASE):gsub("/+$","")
    return cfg
  end

  ui.clear(); ui.header("ATM SETUP","Pair this machine with MineBank")
  local base=ui.prompt("MineBank HTTPS API base",cfg and cfg.api_base or ui.API_BASE)
  if not base or not base:match("^https://") then return nil,"API base must use HTTPS." end
  ui.API_BASE=base:gsub("/+$","")

  ui.clear(); ui.header("ATM SETUP","Operator controls • machine pairing")
  local operatorPhrase=ui.prompt("Type OPERATOR SETUP on the physical computer")
  if operatorPhrase~="OPERATOR SETUP" then return nil,"Setup cancelled." end

  local names=ui.findInventory()
  if #names<3 then
    ui.clear(); ui.header("STORAGE NOT DETECTED","Found "..#names.." usable inventories; 3 are required")
    print("Peripherals visible to this computer:")
    for _,line in ipairs(ui.inventoryDiagnostics()) do print(line) end
    print("")
    print("For remote chests, use a WIRED modem on the computer and")
    print("a wired modem on each chest, all linked by wired modem cable.")
    print("Wireless modems do not expose remote inventories.")
    print("If a chest is not listed above, CC:Tweaked cannot see it.")
    return nil,"Only "..#names.." usable inventory peripherals detected; see the storage diagnostics above."
  end
  local d=ui.choose("DEPOSIT INVENTORY",names,"Inventory that receives deposited Emeralds.")
  if not d or not names[d] then return nil,"Deposit inventory selection cancelled." end
  local s=ui.choose("SECURE STOCK / ESCROW",names,"Inventory for ATM stock and deposit escrow.")
  if not s or not names[s] then return nil,"Secure stock inventory selection cancelled." end
  local o=ui.choose("WITHDRAWAL OUTPUT",names,"Inventory that delivers withdrawn Emeralds.")
  if not o or not names[o] then return nil,"Output inventory selection cancelled." end
  if d==s or d==o or s==o then return nil,"Select three different inventory peripherals." end

  ui.clear(); ui.header("PAIR ATM","Cardholder authorization required")
  ui.message("Insert the MineBank floppy card of the customer pairing this machine. The machine credential will be created automatically; no Vercel environment token is needed.",true)
  ui.autoPause(2.4)
  local card,cardErr=ui.card()
  if not card then return nil,cardErr or "Insert a valid MineBank card before pairing this ATM." end
  local walletPin=ui.secret("Wallet PIN of the cardholder pairing this ATM")
  if not walletPin or not walletPin:match("^%d%d%d%d%d%d$") then return nil,"A valid six-digit Wallet PIN is required for ATM pairing." end
  local label=ui.prompt("ATM display name","ATM "..tostring(os.getComputerID()))
  if not label or not label:match("%S") then label="ATM "..tostring(os.getComputerID()) end

  local enrolled,err,status=ui.api("/atms/enroll","POST",card.credential,
    {wallet_pin=walletPin,label=tostring(label):sub(1,80)},ui.key("atm-enroll"))
  walletPin=nil
  if not enrolled or type(enrolled.atm_token)~="string" or not enrolled.atm_token:match("^mbatm_") then
    return nil,"ATM pairing failed"..(status and (" (HTTP "..tostring(status)..")") or "")..": "..tostring(err or "MineBank returned no machine credential")
  end

  local newCfg={
    api_base=ui.API_BASE,
    atm_token=enrolled.atm_token,
    pairing_version=1,
    atm_id=enrolled.atm_id,
    atm_label=enrolled.label or label,
    deposit_inventory=names[d],
    stock_inventory=names[s],
    output_inventory=names[o],
  }
  if not save(CONFIG,newCfg) then
    return nil,"MineBank paired this machine, but the local config could not be saved. Keep the pairing token from the response private and check computer storage before retrying."
  end
  ui.clear(); ui.header("ATM PAIRED","Ready for MineBank transactions")
  ui.message("This ATM is now paired as "..tostring(newCfg.atm_label)..". Its credential is saved locally and is not shown again.",true)
  ui.autoPause(2.4)
  return newCfg
end
local cfg,cfgErr=ensureConfig()
if not cfg then ui.message(cfgErr,false); ui.autoPause(2.4); return end
if not http then ui.message("Enable the CC:Tweaked HTTP API and allow the MineBank HTTPS host.",false); ui.autoPause(2.4); return end
local depositInv=ui.inventory(cfg.deposit_inventory)
local stockInv=ui.inventory(cfg.stock_inventory)
local outputInv=ui.inventory(cfg.output_inventory)
if not depositInv or not stockInv or not outputInv then ui.message("Configured inventory missing. Run setup again.",false); ui.autoPause(2.4); return end
ui.useTarget(ui.hasMonitor() and "monitor" or "terminal")

local function refundRejectedDeposit(order)
  local stockBefore=tonumber(order.stock_before_refund)
  local depositBefore=tonumber(order.deposit_before_refund)
  if not stockBefore or not depositBefore then
    ui.message("Deposit refund has no saved inventory checkpoint. Do not move Emeralds; operator reconciliation is required.",false)
    ui.autoPause(3.2)
    return false
  end
  local stockNow=countEmeralds(stockInv)
  local depositNow=countEmeralds(depositInv)
  local movedFromStock=stockBefore-stockNow
  local receivedByInput=depositNow-depositBefore
  if movedFromStock<0 or receivedByInput<0 or movedFromStock~=receivedByInput
    or movedFromStock>tonumber(order.amount or 0) then
    ui.message("Deposit refund checkpoint mismatch (stock returned "..tostring(movedFromStock)..", input received "..tostring(receivedByInput).."). No further items were moved.",false)
    ui.autoPause(3.2)
    return false
  end
  local remaining=tonumber(order.amount)-movedFromStock
  if remaining>0 then
    if not canFit(depositInv,"minecraft:emerald",remaining) then
      ui.message("The deposit input lacks space for the remaining refund. Keep the journal and secure stock unchanged until space is available.",false)
      ui.autoPause(3)
      return false
    end
    local returned=moveEmeraldAmount(stockInv,depositInv,remaining,cfg.deposit_inventory)
    if returned~=remaining then
      ui.message("Deposit refund is incomplete ("..tostring(movedFromStock+returned).." of "..tostring(order.amount).."). The REFUND_PENDING journal is retained; resume after checking both inventories.",false)
      ui.autoPause(3.2)
      return false
    end
  end
  if countEmeralds(stockInv)~=stockBefore-tonumber(order.amount)
    or countEmeralds(depositInv)~=depositBefore+tonumber(order.amount) then
    ui.message("The full deposit refund could not be verified against both inventories. Preserve the journal and ask the operator to reconcile it.",false)
    ui.autoPause(3.2)
    return false
  end
  order.state="CANCELLED"
  order.cancelled_at=os.epoch("utc")
  order.failure=tostring(order.rejection or "MineBank rejected the deposit; escrow was returned.")
  if not save(JOURNAL,order) then
    ui.message("The refund is physically complete, but the cancellation journal could not be saved. Keep the ATM stopped and reconcile.",false)
    ui.autoPause(3.2)
    return false
  end
  ui.message("DEPOSIT REJECTED — all "..tostring(order.amount).." Emeralds have been returned to the deposit input. No credit was issued.",false)
  ui.autoPause(3)
  return true
end

local function adoptPendingOperationForSameCustomer(order, card, expectedKind, journalPath)
  -- A replacement card from the same customer may recover an operation that
  -- already reached the bank. The server only returns a receipt scoped to the
  -- current cardholder; a different customer's operation remains inaccessible.
  if not order or not order.key or not card or not card.credential then return false end
  local receipt,err,status=ui.api("/operations/"..tostring(order.key),"GET",card.credential)
  if not receipt or receipt.found~=true then
    ui.message("This pending operation cannot be matched to the inserted cardholder. Insert the original card, or have the operator reconcile the journal and physical inventories. "..tostring(err or ("HTTP "..tostring(status or "?"))),false)
    ui.autoPause(3)
    return false
  end
  if tostring(receipt.operation_type or "")~=tostring(expectedKind or "") then
    ui.message("The bank's operation record does not match the local journal. Stop and reconcile the ATM journal before moving Emeralds.",false)
    ui.autoPause(3)
    return false
  end
  order.card_id=card.card_id
  if not save(journalPath or JOURNAL,order) then
    ui.message("The bank recognizes this operation, but the updated card association could not be saved. Keep the ATM stopped and reconcile the journal.",false)
    ui.autoPause(3)
    return false
  end
  return true
end

local function runWithdrawal(providedCard,providedPin)
  if not providedCard then
    ui.clear(); ui.header("WITHDRAW EMERALDS","Step 1 of 3 • Card and PIN")
    ui.message("Insert your MineBank floppy card, then enter your six-digit Wallet PIN when prompted.",true)
    ui.autoPause(1.6)
  end
  local card=providedCard or getCard(); if not card then return end
  local p=providedPin
  if not p then
    ui.clear(); ui.header("WITHDRAW EMERALDS","Step 2 of 3 • Verify identity")
    p=pin(); if not p then return end
  end
  local old=load(JOURNAL)
  local order
  if old and old.state=="CORRUPT" then reportDamagedJournal(); return end
  if old and old.state~="COMPLETE" and old.state~="CANCELLED" then
    if old.kind~="WITHDRAWAL" or (old.state~="REQUEST_PENDING" and old.state~="AUTHORIZED" and old.state~="DISPENSE_STARTED") then
      ui.message("ATM temporarily locked by "..tostring(old.kind or "unknown").." / "..tostring(old.state or "unknown")..". Open More Services → Resume pending operation, then choose Check MineBank status. The saved operation must be reconciled before a new one can start.",false); ui.autoPause(3.2); return
    end
    if tostring(old.card_id or "")~=tostring(card.card_id or "") then
      if not adoptPendingOperationForSameCustomer(old,card,"WITHDRAWAL",JOURNAL) then return end
    end
    order=old
    if order.state=="DISPENSE_STARTED" then
      local stockBefore=tonumber(order.stock_before_dispense)
      local outputBefore=tonumber(order.output_before_dispense)
      if not stockBefore or not outputBefore then
        ui.message("This withdrawal was debited, but its older journal has no inventory checkpoint. The ATM cannot verify whether the Emeralds were dispensed.",false)
        ui.message("Check the transaction in MineBank history and inspect secure stock/output. Do not start another withdrawal until that physical movement is reconciled.",false)
        ui.autoPause(3.2); return
      end
      local stockMoved=stockBefore-countEmeralds(stockInv)
      local outputAdded=countEmeralds(outputInv)-outputBefore
      if stockMoved==order.amount and outputAdded==order.amount then
        order.state="COMPLETE"; order.completed_at=os.epoch("utc")
        if save(JOURNAL,order) then
          ui.message("The inventory checkpoint confirms this withdrawal was fully dispensed. The journal is now complete; no items were moved again.",true)
        else
          ui.message("The checkpoint confirms the Emeralds were dispensed, but the journal could not be updated. Keep the ATM stopped and reconcile the journal.",false)
        end
        ui.autoPause(3); return
      elseif stockMoved~=0 or outputAdded~=0 then
        ui.message("Physical inventory changed after dispensing began (stock change: "..tostring(stockMoved).."; output change: "..tostring(outputAdded).."). No Emeralds were moved automatically.",false)
        ui.message("This may be a partial dispense or customer collection. Operator reconciliation is required before continuing.",false)
        ui.autoPause(3.2); return
      end
      -- Both inventories match the saved pre-dispense checkpoint. Continue
      -- using the already-authorized ticket; do not send a second withdrawal.
    end
  else
    ui.clear(); ui.header("WITHDRAW EMERALDS","Step 3 of 4 • Choose amount")
    local amountChoices={"10 Emeralds","32 Emeralds","64 Emeralds","128 Emeralds","Enter another amount","Cancel"}
    local selected=ui.choose("WITHDRAW EMERALDS",amountChoices,"Maximum per withdrawal: "..MAX_WITHDRAWAL.." Emeralds.")
    local amount
    if selected==1 then amount=10
    elseif selected==2 then amount=32
    elseif selected==3 then amount=64
    elseif selected==4 then amount=128
    elseif selected==5 then
      local raw=ui.prompt("Whole Emerald amount (1-"..MAX_WITHDRAWAL..")")
      amount=raw and tonumber(raw) or nil
    else
      ui.message("Withdrawal cancelled. No debit requested.",true); ui.autoPause(1.5); return
    end
    if not amount or amount%1~=0 or amount<1 or amount>MAX_WITHDRAWAL then ui.message("Invalid amount. Enter a whole number from 1 to "..MAX_WITHDRAWAL..". No debit was requested.",false); ui.autoPause(2.4); return end
    if countEmeralds(stockInv)<amount then ui.message("Secure stock does not contain enough emeralds.",false); ui.autoPause(2.4); return end
    if not canFit(outputInv,"minecraft:emerald",amount) then ui.message("Output inventory lacks space. No debit requested.",false); ui.autoPause(2.4); return end
    local confirm=ui.choose("CONFIRM WITHDRAWAL",{"Withdraw "..amount.." Emeralds","Cancel"},"Your MineBank account will be debited before dispensing.")
    if confirm~=1 then ui.message("Withdrawal cancelled. No debit requested.",true); ui.autoPause(2.4); return end
    order={version=1,key=ui.key("withdraw"),amount=amount,state="REQUEST_PENDING",kind="WITHDRAWAL",card_id=card.card_id}
    if not save(JOURNAL,order) then ui.message("Could not persist journal. No request sent.",false); ui.autoPause(2.4); return end
  end
  if countEmeralds(stockInv)<order.amount then ui.message("Secure stock is short. The saved withdrawal has not been re-requested.",false); ui.autoPause(2.4); return end
  if not canFit(outputInv,"minecraft:emerald",order.amount) then ui.message("Output inventory lacks space. No new debit requested.",false); ui.autoPause(2.4); return end
  local result
  if order.state=="REQUEST_PENDING" then
    ui.clear(); ui.header("WITHDRAWAL IN PROGRESS","Authorising with MineBank")
    ui.message("Requested amount: "..tostring(order.amount).." Emeralds.",true)
    ui.message("Checking your account and recording this request. Do not restart the ATM.",true)
    local err,status
    result,err,status=ui.api("/withdrawals","POST",card.credential,{amount=order.amount,wallet_pin=p},order.key,{["X-MineBank-ATM-Token"]=cfg.atm_token})
    if not result then
      if status==0 or (status and status>=400 and status<500) then
        cancelRejected(order,err)
        ui.message("Withdrawal rejected by MineBank. No Emeralds were dispensed; this attempt is cancelled. "..tostring(err),false)
      else
        ui.message("Withdrawal outcome is unknown: "..tostring(err)..". The same idempotency key is retained; retry this withdrawal to reconcile it.",false)
      end
      ui.autoPause(2.4); return
    end
    order.state="AUTHORIZED"; order.transaction_id=result.transaction_id
    if not save(JOURNAL,order) then ui.message("MineBank debited the account but the local ticket could not be saved. Do not dispense.",false); ui.autoPause(2.4); return end
  else
    result={transaction_id=order.transaction_id,amount=order.amount}
  end
  if order.state~="DISPENSE_STARTED" then
    order.stock_before_dispense=countEmeralds(stockInv)
    order.output_before_dispense=countEmeralds(outputInv)
    order.state="DISPENSE_STARTED"
    if not save(JOURNAL,order) then ui.message("Could not lock the dispense journal. No items moved; operator reconciliation required.",false); ui.autoPause(2.4); return end
  end
  local remaining=order.amount
  for slot,detail in pairs(stockInv.list()) do
    if detail.name=="minecraft:emerald" and remaining>0 then
      local moved=stockInv.pushItems(cfg.output_inventory,slot,math.min(remaining,detail.count))
      remaining=remaining-moved
      if moved<=0 then break end
    end
  end
  if remaining>0 then ui.message("Partial dispense detected. Do not retry. Operator reconciliation required.",false); ui.autoPause(2.4); return end
  order.state="COMPLETE"; order.completed_at=os.epoch("utc")
  if not save(JOURNAL,order) then ui.message("Items moved but journal completion failed. Lock ATM and reconcile.",false); ui.autoPause(2.4); return end
  ui.message("WITHDRAWAL COMPLETE — "..order.amount.." Emeralds",true)
  print("Transaction: "..tostring(result.transaction_id)); ui.autoPause(2.4)
end

local function runDeposit()
  ui.clear(); ui.header("DEPOSIT EMERALDS","Step 1 of 3 • Card and PIN")
  ui.message("Insert your MineBank floppy card, then enter your six-digit Wallet PIN when prompted.",true)
  ui.autoPause(1.6)
  local card=getCard(); if not card then return end
  ui.clear(); ui.header("DEPOSIT EMERALDS","Step 2 of 3 • Verify identity")
  local p=pin(); if not p then return end
  local old=load(JOURNAL)
  if old and old.state=="CORRUPT" then reportDamagedJournal(); return end
  local resuming=old and old.state~="COMPLETE" and old.state~="CANCELLED"
  if not resuming then
    ui.clear(); ui.header("DEPOSIT EMERALDS","Step 3 of 3 • Confirm deposit")
    local ready=ui.choose("DEPOSIT EMERALDS",{"Count Emeralds in deposit slot","Cancel"},"Place the Emeralds you want to deposit in the deposit slot, then continue.")
    if ready~=1 then ui.message("Deposit cancelled. No Emeralds moved.",true); ui.autoPause(2.4); return end
  end
  local order
  if old and old.state~="COMPLETE" and old.state~="CANCELLED" then
    if old.kind~="DEPOSIT" or (old.state~="REQUEST_PENDING" and old.state~="ESCROW_PENDING" and old.state~="REFUND_PENDING") then
      ui.message("ATM temporarily locked by "..tostring(old.kind or "unknown").." / "..tostring(old.state or "unknown")..". Open More Services → Resume pending operation, then choose Check MineBank status. The saved operation must be reconciled before a new one can start.",false); ui.autoPause(3.2); return
    end
    if tostring(old.card_id or "")~=tostring(card.card_id or "") then
      if old.state=="ESCROW_PENDING" or old.state=="REFUND_PENDING" or not adoptPendingOperationForSameCustomer(old,card,"DEPOSIT",JOURNAL) then return end
    end
    order=old
  else
    local returned=returnInvalidItems(depositInv,cfg.output_inventory)
    local amount=countEmeralds(depositInv)
    if amount<1 then
      ui.message("No Emeralds found. Non-Emerald items were returned to output when space allowed ("..returned.." items).",false)
      ui.autoPause(2.4); return
    end
    if amount>4096 then ui.message("Deposit is limited to 4,096 Emeralds per operation.",false); ui.autoPause(2.4); return end
    if not canFit(stockInv,"minecraft:emerald",amount) then ui.message("Secure stock inventory lacks space. No items moved.",false); ui.autoPause(2.4); return end
    local confirm=ui.choose("CONFIRM DEPOSIT",{"Deposit "..amount.." Emeralds","Cancel"},"Detected "..amount.." Emeralds. Confirm to secure and credit them.")
    if confirm~=1 then ui.message("Deposit cancelled. No Emeralds moved.",true); ui.autoPause(2.4); return end
    order={version=2,key=ui.key("deposit"),amount=amount,state="ESCROW_PENDING",kind="DEPOSIT",card_id=card.card_id,deposit_before=countEmeralds(depositInv),stock_before_escrow=countEmeralds(stockInv)}
    if not save(JOURNAL,order) then ui.message("Could not persist deposit journal; items left untouched.",false); ui.autoPause(2.4); return end
  end
  if order.state=="REFUND_PENDING" then
    refundRejectedDeposit(order)
    return
  end
  if order.state=="ESCROW_PENDING" then
    -- Recover interrupted physical escrow transfers. The journal is persisted
    -- before movement, so compare both sides against that checkpoint instead of
    -- assuming an all-or-nothing inventory push.
    local depositBefore=tonumber(order.deposit_before)
    local stockBefore=tonumber(order.stock_before_escrow)
    if not depositBefore or not stockBefore then
      -- Legacy journals have no checkpoint. If the entire original amount is
      -- still in the input, no part of this operation appears to have moved;
      -- establish a fresh checkpoint. Partial legacy movement remains manual.
      if countEmeralds(depositInv)~=tonumber(order.amount) then
        ui.message("This older deposit journal has no escrow checkpoint and the input no longer contains the full amount. Do not move Emeralds; operator reconciliation is required.",false)
        ui.autoPause(3.2)
        return
      end
      order.deposit_before=countEmeralds(depositInv)
      order.stock_before_escrow=countEmeralds(stockInv)
      depositBefore=order.deposit_before
      stockBefore=order.stock_before_escrow
      if not save(JOURNAL,order) then
        ui.message("Could not save the escrow recovery checkpoint. No Emeralds were moved.",false)
        ui.autoPause(2.4)
        return
      end
    end
    local depositNow=countEmeralds(depositInv)
    local stockNow=countEmeralds(stockInv)
    local depositMoved=depositBefore-depositNow
    local stockAdded=stockNow-stockBefore
    if depositMoved<0 or stockAdded<0 or depositMoved~=stockAdded or depositMoved>order.amount then
      ui.message("Deposit inventory checkpoint mismatch (input moved "..tostring(depositMoved)..", secure stock added "..tostring(stockAdded).."). No further Emeralds were moved; operator reconciliation is required.",false)
      ui.autoPause(3.2)
      return
    end
    local remaining=order.amount-depositMoved
    if remaining>0 then
      local moved=moveEmeraldAmount(depositInv,stockInv,remaining,cfg.stock_inventory)
      if moved~=remaining then
        ui.message("Escrow transfer is incomplete ("..tostring(depositMoved+moved).." of "..tostring(order.amount).."). The journal is retained; retry recovery only after checking both inventories.",false)
        ui.autoPause(3.2)
        return
      end
    end
    if countEmeralds(depositInv)~=depositBefore-order.amount
      or countEmeralds(stockInv)~=stockBefore+order.amount then
      ui.message("Escrow movement could not be verified against both inventory checkpoints. The bank request was not sent; operator reconciliation is required.",false)
      ui.autoPause(3.2)
      return
    end
    order.state="REQUEST_PENDING"
    if not save(JOURNAL,order) then ui.message("Emeralds are secured in escrow but journal update failed. Do not move them.",false); ui.autoPause(2.4); return end
  end
  local result,err,status=ui.api("/deposits","POST",card.credential,{amount=order.amount,wallet_pin=p},order.key,{["X-MineBank-ATM-Token"]=cfg.atm_token})
  if not result then
    if status==0 or (status and status>=400 and status<500) then
      -- Persist refund intent and both inventory checkpoints before returning
      -- any escrow. A restart can resume the refund without resubmitting a
      -- bank request or returning the same amount twice.
      order.state="REFUND_PENDING"
      order.rejection=tostring(err or "MineBank rejected the deposit.")
      order.stock_before_refund=countEmeralds(stockInv)
      order.deposit_before_refund=countEmeralds(depositInv)
      if not save(JOURNAL,order) then
        ui.message("MineBank rejected the deposit, but refund intent could not be saved. Do not move Emeralds; operator reconciliation is required.",false)
        ui.autoPause(3.2)
        return
      end
      refundRejectedDeposit(order)
    else
      ui.message("Deposit outcome is unknown: "..tostring(err)..". Emeralds remain in secure stock; check MineBank status or retry the same saved operation. Do not move escrow manually.",false)
      ui.autoPause(3)
    end
    return
  end
  order.state="COMPLETE"; order.transaction_id=result.transaction_id; order.completed_at=os.epoch("utc")
  if not save(JOURNAL,order) then ui.message("MineBank confirmed the deposit but journal update failed. Preserve the operation key and reconcile.",false); ui.autoPause(2.4); return end
  ui.message("DEPOSIT COMPLETE — "..order.amount.." Emeralds",true)
  print("Transaction: "..tostring(result.transaction_id)); ui.autoPause(2.4)
end

local function runTransfer()
  local card=getCard(); if not card then return end
  local p=pin(); if not p then return end
  local old=load(TRANSFER_JOURNAL)
  local order
  if old and old.state=="CORRUPT" then reportDamagedJournal(); return end
  if old and old.state~="COMPLETE" and old.state~="CANCELLED" then
    if old.kind~="TRANSFER" or old.state~="REQUEST_PENDING" then
      ui.message("ATM temporarily locked by "..tostring(old.kind or "unknown").." / "..tostring(old.state or "unknown")..". Open More Services → Resume pending operation, then choose Check MineBank status. The saved operation must be reconciled before a new one can start.",false); ui.autoPause(3.2); return
    end
    if tostring(old.card_id or "")~=tostring(card.card_id or "") then
      if not adoptPendingOperationForSameCustomer(old,card,"TRANSFER",TRANSFER_JOURNAL) then return end
    end
    order=old
  else
    local recipient=ui.prompt("Recipient account number")
    local amount=tonumber(ui.prompt("Amount in Emeralds (1-1000)"))
    if not amount or amount%1~=0 or amount<1 or amount>MAX_TRANSFER then ui.message("Transfers are limited to 1,000 Emeralds. Use the portal for larger transfers.",false); ui.autoPause(2.4); return end
    local confirm=ui.choose("CONFIRM TRANSFER",{"Transfer "..amount.." Emeralds","Cancel"},"Recipient: "..recipient)
    if confirm~=1 then ui.message("Cancelled. No transfer submitted.",true); ui.autoPause(2.4); return end
    order={version=1,key=ui.key("transfer"),amount=amount,recipient=recipient,state="REQUEST_PENDING",kind="TRANSFER",card_id=card.card_id}
    if not save(TRANSFER_JOURNAL,order) then ui.message("Could not save transfer journal; no request sent.",false); ui.autoPause(2.4); return end
  end
  local result,err,status=ui.api("/transfers","POST",card.credential,
    {recipient_account_number=order.recipient,amount=order.amount,wallet_pin=p},order.key)
  if not result then
    if status==0 or (status and status>=400 and status<500) then
      cancelRejected(order,err,TRANSFER_JOURNAL)
      ui.message("MineBank rejected the transfer. No transfer was accepted; this attempt is cancelled. "..tostring(err),false)
    else
      ui.message("Transfer outcome is unknown: "..tostring(err)..". The original idempotency key is retained; retry this operation to reconcile it.",false)
    end
    ui.autoPause(2.4); return
  end
  order.state="COMPLETE"; order.transaction_id=result.transaction_id; order.completed_at=os.epoch("utc")
  if not save(TRANSFER_JOURNAL,order) then ui.message("MineBank confirmed the transfer but the journal update failed. Preserve the key and reconcile.",false); ui.autoPause(2.4); return end
  ui.message("TRANSFER COMPLETE — "..order.amount.." Emeralds",true)
  print("Transaction: "..tostring(result.transaction_id)); print("Fee: "..tostring(result.fee or 0)); ui.autoPause(2.4)
end

local function runBalance()
  local card=getCard(); if not card then return end
  local result,err=ui.api("/account","GET",card.credential)
  if not result then
    ui.clear(); ui.header("BALANCE UNAVAILABLE")
    ui.message("MineBank could not confirm your account: "..tostring(err),false)
    ui.autoPause(2.4); return
  end
  local account=result.account or {}
  ui.clear(); ui.header("ACCOUNT OVERVIEW","Live data returned by MineBank")
  print("Account: "..tostring(account.account_number or "—"))
  print("Type: "..tostring(account.account_type or "—"))
  print("Tier: "..tostring(account.tier or "—"))
  print("Status: "..tostring(account.status or "—"))
  print("")
  print("AVAILABLE BALANCE")
  print(tostring(account.balance or 0).." Emeralds")
  ui.autoPause(2.4)
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
  ui.autoPause(2.4)
end

local function runRevoke()
  local card=getCard(); if not card then return end
  local confirm=ui.choose("REVOKE CARD",{"Permanently revoke card","Cancel"},"Card "..tostring(card.card_id or "unknown")..". A new card will be required.")
  if confirm~=1 then ui.message("Cancelled. The card remains active.",true); ui.autoPause(2.4); return end
  local p=pin(); if not p then return end
  local result,err=ui.api("/cards/revoke-self","POST",card.credential,{wallet_pin=p,confirm=true})
  if not result then ui.message("Revocation not confirmed: "..tostring(err),false)
  else ui.message("CARD REVOKED. This floppy can no longer be used.",true) end
  ui.autoPause(2.4)
end

local function runRecovery()
  local physical=load(JOURNAL)
  local transfer=load(TRANSFER_JOURNAL)
  local function pending(value)
    return value and value.state~="COMPLETE" and value.state~="CANCELLED"
  end
  local order,journalPath
  if pending(physical) and pending(transfer) then
    local selected=ui.choose("RECOVERY QUEUE",{
      "Physical "..tostring(physical.kind or "operation").." • "..tostring(physical.state or "unknown"),
      "Transfer • "..tostring(transfer.state or "unknown"),
      "Return to ATM"
    },"Resolve one saved operation at a time.")
    if selected==1 then order,journalPath=physical,JOURNAL
    elseif selected==2 then order,journalPath=transfer,TRANSFER_JOURNAL
    else return end
  elseif pending(physical) then
    order,journalPath=physical,JOURNAL
  elseif pending(transfer) then
    order,journalPath=transfer,TRANSFER_JOURNAL
  else
    ui.message("No unfinished ATM operation needs recovery.",true); ui.autoPause(2.4); return
  end
  if order.state=="CORRUPT" then
    reportDamagedJournal()
    return
  end

  local summary=tostring(order.kind or "UNKNOWN").." • "..tostring(order.amount or "?")..
    " Emeralds • "..tostring(order.state or "UNKNOWN")
  local action=ui.choose("OPERATION RECOVERY",{
    "Check MineBank status",
    "Resume the saved operation",
    "Cancel safe unstarted deposit",
    "Return to ATM"
  },summary)

  if action==1 then
    local card=getCard()
    if not card then return end
    ui.clear(); ui.header("RECOVERY CHECK","Checking the bank's authoritative record")
    local receipt,err,status=ui.api("/operations/"..tostring(order.key or ""),"GET",card.credential)
    if not receipt or receipt.found~=true then
      ui.message("MineBank could not match this operation to the inserted card: "..tostring(err or ("HTTP "..tostring(status or "?"))),false)
      ui.message("A not-found response does not prove the operation was never recorded for another customer. Keep the journal and inventories unchanged; try the original card or ask the operator to reconcile it.",false)
      ui.autoPause(4)
      return
    end
    if tostring(receipt.operation_type or "")~=tostring(order.kind or "") then
      ui.message("The bank operation type differs from the local journal. Do not move Emeralds; operator reconciliation is required.",false)
      ui.autoPause(3.5)
      return
    end
    if tostring(receipt.status or ""):upper()~="COMPLETED" then
      ui.message("MineBank returned an operation record that is not confirmed complete. No physical items were moved.",false)
      ui.autoPause(3)
      return
    end

    local bankResponse=receipt.response or {}
    local transactionId=receipt.transaction_id or bankResponse.transaction_id or order.transaction_id
    if order.kind=="TRANSFER" then
      order.state="COMPLETE"
      order.transaction_id=transactionId
      order.completed_at=os.epoch("utc")
      order.recovered_from_bank=true
      if save(journalPath,order) then
        ui.message("TRANSFER RECONCILED — MineBank confirms transaction "..tostring(transactionId or "recorded")..". No transfer was submitted again.",true)
      else
        ui.message("MineBank confirms the transfer, but the journal could not be updated. Keep the ATM stopped and preserve the journal.",false)
      end
      ui.autoPause(3)
      return
    elseif order.kind=="DEPOSIT" then
      if order.state~="REQUEST_PENDING" then
        ui.message("MineBank confirms a deposit, but the local journal is not at the expected post-escrow stage. Do not move stock; operator reconciliation is required.",false)
        ui.autoPause(3.5)
        return
      end
      order.state="COMPLETE"
      order.transaction_id=transactionId
      order.completed_at=os.epoch("utc")
      order.recovered_from_bank=true
      if save(journalPath,order) then
        ui.message("DEPOSIT RECONCILED — MineBank confirms the credit. Emeralds remain in secure stock; do not return or deposit them again.",true)
      else
        ui.message("MineBank confirms the deposit, but the journal could not be updated. Preserve the journal and escrow.",false)
      end
      ui.autoPause(3)
      return
    elseif order.kind=="WITHDRAWAL" then
      if order.state~="REQUEST_PENDING" and order.state~="AUTHORIZED" and order.state~="DISPENSE_STARTED" then
        ui.message("MineBank confirms a withdrawal, but the local journal stage is unexpected. Stop and reconcile the physical inventories.",false)
        ui.autoPause(3.5)
        return
      end
      order.transaction_id=transactionId
      if order.state=="REQUEST_PENDING" then order.state="AUTHORIZED" end
      order.recovered_from_bank=true
      if not save(journalPath,order) then
        ui.message("MineBank confirms the debit, but the recovery ticket could not be saved. Do not dispense; preserve the journal.",false)
        ui.autoPause(3)
        return
      end
      ui.clear(); ui.header("WITHDRAWAL RECOVERY","Bank confirmed • identity check required")
      ui.message("MineBank confirms this withdrawal. Enter the Wallet PIN to resume the saved dispense without creating another debit.",true)
      local p=pin()
      if not p then return end
      runWithdrawal(card,p)
      return
    else
      ui.message("Unknown operation type. No changes were made; operator reconciliation is required.",false)
      ui.autoPause(3)
      return
    end

  elseif action==2 then
    if order.kind=="WITHDRAWAL" then runWithdrawal()
    elseif order.kind=="DEPOSIT" then runDeposit()
    elseif order.kind=="TRANSFER" then runTransfer()
    else ui.message("Unknown journal type. Operator reconciliation is required.",false); ui.autoPause(2.4) end
  elseif action==3 then
    if order.kind=="DEPOSIT" and order.state=="ESCROW_PENDING" and countEmeralds(depositInv)>=tonumber(order.amount or 0) then
      local confirm=ui.choose("CANCEL DEPOSIT",{"Cancel unstarted deposit","Keep pending"},"Confirm that the Emeralds are still in the deposit input and no items have moved.")
      if confirm==1 then
        order.state="CANCELLED"; order.cancelled_at=os.epoch("utc"); order.failure="Cancelled before escrow movement by operator."
        if save(journalPath,order) then ui.message("Deposit cancelled. No MineBank request was sent.",true)
        else ui.message("Could not save cancellation. Keep the machine stopped and reconcile.",false) end
      end
    else
      ui.message("This operation may already have reached MineBank or moved physical Emeralds. It cannot be cancelled safely here. Check MineBank status, resume with the original card, or reconcile the journal and inventory.",false)
    end
    ui.autoPause(2.4)
  end
end

local function runDiagnostics()
  ui.clear(); ui.header("DIAGNOSTICS")
  print("API: "..ui.API_BASE)
  print("Deposit inventory: "..cfg.deposit_inventory)
  print("Secure stock/escrow: "..cfg.stock_inventory)
  print("Output inventory: "..cfg.output_inventory)
  print("Deposit Emeralds: "..countEmeralds(depositInv))
  print("Stock Emeralds: "..countEmeralds(stockInv))
  print("Output Emeralds: "..countEmeralds(outputInv))
  local journal=load(JOURNAL)
  if not journal then
    print("Local operation journal: no unfinished operation")
  elseif journal.state=="CORRUPT" then
    print("Local operation journal: DAMAGED (not proof of a pending bank operation)")
  else
    print("Local operation journal: "..tostring(journal.kind or "UNKNOWN").." / "..tostring(journal.state or "UNKNOWN").." / "..tostring(journal.amount or "?").." Emeralds")
  end
  local transferJournal=load(TRANSFER_JOURNAL)
  if not transferJournal then
    print("Transfer journal: no unfinished transfer")
  elseif transferJournal.state=="CORRUPT" then
    print("Transfer journal: DAMAGED")
  else
    print("Transfer journal: "..tostring(transferJournal.state or "UNKNOWN").." / "..tostring(transferJournal.amount or "?").." Emeralds")
  end
  print("HTTP enabled: "..tostring(http~=nil))
  print("Modem detected (optional): "..tostring(peripheral.find("modem")~=nil))
  print("Monitor detected: "..tostring(peripheral.find("monitor")~=nil))
  ui.autoPause(2.4)
end

while true do
  local ok,choice,pending=pcall(draw,cfg)
  if not ok then
    ui.clear(); ui.header("ATM INTERFACE ERROR","The ATM recovered without submitting a transaction")
    ui.message(tostring(choice),false); ui.message("Choose Exit/restart only after checking the operation journal.",false)
    ui.autoPause(3)
  elseif not choice then
    break
  else
    local actionOk,actionResult=pcall(function()
      if ui.hasMonitor() then
        if pending then
          if choice==1 then runRecovery()
          elseif choice==2 then runBalance()
          elseif choice==3 then
            local more=drawMore()
            if more==1 then runTransfer()
            elseif more==2 then runHistory()
            elseif more==3 then runRevoke()
            elseif more==4 then runDiagnostics()
            elseif more==5 then runRecovery()
            end
          end
        else
          if choice==1 then runBalance()
          elseif choice==2 then runWithdrawal()
          elseif choice==3 then runDeposit()
          elseif choice==4 then
            local more=drawMore()
            if more==1 then runTransfer()
            elseif more==2 then runHistory()
            elseif more==3 then runRevoke()
            elseif more==4 then runDiagnostics()
            elseif more==5 then runRecovery()
            end
          end
        end
      else
        if choice==1 then runBalance()
        elseif choice==2 then runWithdrawal()
        elseif choice==3 then runDeposit()
        elseif choice==4 then runTransfer()
        elseif choice==5 then runHistory()
        elseif choice==6 then runRevoke()
        elseif choice==7 then runDiagnostics()
        elseif choice==8 then runRecovery()
        else return "EXIT" end
      end
    end)
    if actionOk and actionResult=="EXIT" then break end
    if not actionOk then
      ui.clear(); ui.header("ATM OPERATION INTERRUPTED","No silent failure")
      ui.message("The ATM caught an interface/runtime error: "..tostring(actionResult),false)
      ui.message("If a transaction may have started, use More services → Resume pending operation before trying again.",false)
      ui.autoPause(4)
    end
  end
end
ui.clear(); ui.header("MINEBANK ATM","Thank you for banking with MineBank.")
