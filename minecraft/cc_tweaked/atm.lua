-- MineBank ATM for CC:Tweaked turtles.
-- Requires a turtle stocked with minecraft:emerald items.
-- Physical dispensing is fail-closed: if interrupted after dispensing begins,
-- the durable journal blocks automatic replay until an operator reconciles it.

local API_BASE = "https://muoc-pietroaccordi09-4057.vercel.app/api/v1/minecraft"
local CARD_FILE = "minebank.card"
local JOURNAL = "minebank.atm_journal"
local MAX_WITHDRAWAL = 4096

local function pause()
  print("")
  print("Press Enter to continue.")
  read()
end

local function writeJournal(value)
  local h = fs.open(JOURNAL .. ".tmp", "w")
  if not h then return false end
  local ok = pcall(function() h.write(textutils.serializeJSON(value)); h.close() end)
  if not ok then pcall(function() h.close() end); fs.delete(JOURNAL .. ".tmp"); return false end
  local backup = JOURNAL .. ".bak"
  if fs.exists(backup) then fs.delete(backup) end
  if fs.exists(JOURNAL) then fs.move(JOURNAL, backup) end
  local moved, moveError = pcall(function() fs.move(JOURNAL .. ".tmp", JOURNAL) end)
  if not moved then return false end
  if fs.exists(backup) then fs.delete(backup) end
  return true
end

local function readJournal()
  if not fs.exists(JOURNAL) then
    if fs.exists(JOURNAL .. ".tmp") or fs.exists(JOURNAL .. ".bak") then
      return nil, "ATM journal recovery files exist. Stop and reconcile manually."
    end
    return nil
  end
  local h = fs.open(JOURNAL, "r")
  if not h then return nil, "The ATM journal cannot be opened." end
  local raw = h.readAll()
  h.close()
  local ok, data = pcall(textutils.unserializeJSON, raw)
  if not ok or type(data) ~= "table" then return nil, "The ATM journal is corrupt. Stop and reconcile manually." end
  return data
end

local function loadCard()
  local drive = peripheral.find("drive")
  if not drive or not drive.isDiskPresent or not drive.isDiskPresent() then return nil, "Insert a MineBank card floppy." end
  local mount = drive.getMountPath and drive.getMountPath()
  if not mount or not fs.exists(mount) then return nil, "The card floppy is not mounted." end
  local path = fs.combine(mount, CARD_FILE)
  if not fs.exists(path) then return nil, "No MineBank card was found on this floppy." end
  local h = fs.open(path, "r")
  if not h then return nil, "Cannot read the card file." end
  local raw = h.readAll(); h.close()
  local ok, card = pcall(textutils.unserializeJSON, raw)
  if not ok or type(card) ~= "table" or type(card.credential) ~= "string" or not card.credential:find("^mbmc_") then
    return nil, "Invalid MineBank card file."
  end
  return card
end

local function apiPost(path, cardToken, body, idempotencyKey)
  local headers = { ["Accept"]="application/json", ["Content-Type"]="application/json" }
  if cardToken then headers["Authorization"] = "Bearer " .. cardToken end
  if idempotencyKey then headers["Idempotency-Key"] = idempotencyKey end
  local response, err = http.post(API_BASE .. path, textutils.serializeJSON(body), headers)
  if not response then return nil, tostring(err or "network error") end
  local status = response.getResponseCode and response.getResponseCode() or 200
  local raw = response.readAll(); response.close()
  local ok, data = pcall(textutils.unserializeJSON, raw)
  if not ok or type(data) ~= "table" then return nil, "Invalid MineBank response (HTTP " .. status .. ")." end
  if status < 200 or status >= 300 then return nil, tostring(data.error or ("HTTP " .. status)) end
  return data
end

local function emeraldCount()
  local total = 0
  for slot = 1, 16 do
    local item = turtle.getItemDetail(slot)
    if item and item.name == "minecraft:emerald" then total = total + item.count end
  end
  return total
end

local function dispense(amount)
  local remaining = amount
  for slot = 1, 16 do
    if remaining <= 0 then break end
    local item = turtle.getItemDetail(slot)
    if item and item.name == "minecraft:emerald" then
      turtle.select(slot)
      local toDrop = math.min(remaining, item.count, 64)
      local before = turtle.getItemCount(slot)
      local ok = turtle.drop(toDrop)
      local after = turtle.getItemCount(slot)
      if not ok then return false, "The turtle could not dispense emeralds. Do not retry automatically." end
      local moved = before - after
      if moved <= 0 or moved > toDrop then return false, "The dispense count could not be verified. Do not retry automatically." end
      remaining = remaining - moved
      if moved < toDrop then return false, "The output accepted only part of the withdrawal. Do not retry automatically." end
    end
  end
  if remaining > 0 then return false, "Dispensing stopped early. Do not retry automatically." end
  return true
end

term.clear(); term.setCursorPos(1,1)
print("MineBank | ATM")
print("The New Bank")
print("------------------------------")
if not API_BASE:match("^https://") or API_BASE:find("YOUR%-MINEBANK%-DOMAIN") then
  print("SETUP ERROR: configure the HTTPS API_BASE."); pause(); return
end
if not http then print("Enable the CC:Tweaked HTTP API."); pause(); return end
if not turtle then print("This ATM program must run on a turtle to dispense items."); pause(); return end

local old, journalError = readJournal()
if journalError then print(journalError); pause(); return end
if old and old.state ~= "COMPLETE" and old.state ~= "REQUEST_PENDING" and old.state ~= "AUTHORIZED" then
  print("ATM RECONCILIATION REQUIRED")
  print("Physical dispensing had started and cannot safely be replayed.")
  print("Withdrawal key: " .. tostring(old.key))
  print("State: " .. tostring(old.state))
  print("An operator must reconcile the physical item output and MineBank ledger.")
  pause(); return
end

local card, cardError = loadCard()
if not card then print(cardError); pause(); return end

local amount, key, journal, result
if old and old.state == "REQUEST_PENDING" then
  amount, key, journal = old.amount, old.key, old
  print("Resuming the same pending request: " .. tostring(key))
elseif old and old.state == "AUTHORIZED" then
  amount, key, journal = old.amount, old.key, old
  result = { transaction_id=old.transaction_id, withdrawal_id=old.withdrawal_id, amount=old.amount }
  print("Resuming an authorized withdrawal: " .. tostring(key))
else
  write("Emeralds to withdraw (1-" .. MAX_WITHDRAWAL .. "): ")
  local amountText = read()
  amount = tonumber(amountText)
  if not amount or amount % 1 ~= 0 or amount < 1 or amount > MAX_WITHDRAWAL then
    print("Enter a whole Emerald amount within the permitted limit."); pause(); return
  end
  amount = math.floor(amount)
  key = tostring(os.epoch("utc")) .. "-" .. tostring(os.getComputerID()) .. "-" .. tostring(math.random(100000,999999))
  journal = { version=1, key=key, amount=amount, state="REQUEST_PENDING", created_at=os.epoch("utc") }
  if not writeJournal(journal) then print("Could not persist the ATM journal. No request was sent."); pause(); return end
end

if emeraldCount() < amount then
  print("ATM inventory is short. Stock at least " .. amount .. " Emeralds before resuming."); pause(); return
end

if journal.state == "REQUEST_PENDING" then
  print("")
  print("Requesting " .. amount .. " Emeralds from MineBank...")
  local err
  result, err = apiPost("/withdrawals", card.credential, {amount=amount}, key)
  if not result then
    print("Withdrawal not confirmed: " .. err)
    print("The same request key is saved. Re-run to safely retry this exact request.")
    pause(); return
  end
  journal.state = "AUTHORIZED"
  journal.transaction_id = result.transaction_id
  journal.withdrawal_id = result.withdrawal_id
  if not writeJournal(journal) then
    print("MineBank debited the balance, but the local ticket could not be saved.")
    print("Do not dispense. Preserve the journal and contact the operator.")
    pause(); return
  end
end

print("MineBank debit confirmed: " .. tostring(result.transaction_id))
print("Preparing the physical dispense...")
journal.state = "DISPENSE_STARTED"
if not writeJournal(journal) then
  print("Could not persist the dispense lock. No items were dispensed.")
  print("Balance is already debited; contact the operator for reconciliation.")
  pause(); return
end

local ok, dispenseError = dispense(amount)
if not ok then
  print("DISPENSE ERROR: " .. dispenseError)
  print("The withdrawal remains debited. Do not retry; operator reconciliation is required.")
  pause(); return
end
journal.state = "COMPLETE"
journal.completed_at = os.epoch("utc")
if not writeJournal(journal) then
  print("Items were dispensed, but completion could not be recorded.")
  print("The ATM is locked against another withdrawal until operator reconciliation.")
  pause(); return
end
print("")
print("WITHDRAWAL COMPLETE")
print("Transaction: " .. tostring(result.transaction_id))
print("Dispensed: " .. amount .. " Emerald items")
print("Balance was deducted by MineBank before dispensing.")
pause()
