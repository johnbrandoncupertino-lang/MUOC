-- MineBank MineClubs entrance controller for CC:Tweaked.
-- Fail-closed: redstone is enabled only after the API confirms ADMITTED.
-- The pending idempotency key is persisted before the request. If the response is
-- lost, resume with the same customer card and key; the server safely replays it.

local CONFIG = "minebank_club_entrance.json"
local JOURNAL = "minebank_club_entrance_journal.json"
local API_PATH = "/clubs/admissions"

local function readJson(path)
  if not fs.exists(path) then return nil end
  local h = fs.open(path, "r")
  if not h then return nil end
  local raw = h.readAll()
  h.close()
  local ok, value = pcall(textutils.unserializeJSON, raw)
  if ok and type(value) == "table" then return value end
  return nil
end

local function writeJson(path, value)
  local h = fs.open(path, "w")
  if not h then error("Cannot write " .. path) end
  h.write(textutils.serializeJSON(value))
  h.close()
end

local function say(message)
  print("")
  print(message)
end

local function failClosed(side)
  if side and redstone then pcall(redstone.setOutput, side, false) end
end

local cfg = readJson(CONFIG)
if not cfg then
  say("Missing or invalid " .. CONFIG .. ". Copy the example from the MineBank repository.")
  return
end

local apiBase = tostring(cfg.api_base or ""):gsub("/+$", "")
local terminalToken = tostring(cfg.club_token or "")
local redstoneSide = tostring(cfg.redstone_side or "")
local terminalName = tostring(cfg.terminal_name or "Trusted entrance terminal")
local pulseSeconds = tonumber(cfg.door_pulse_seconds) or 2
if not apiBase:match("^https://") or not apiBase:match("/api/v1/minecraft$") then
  say("api_base must be HTTPS and end with /api/v1/minecraft.")
  return
end
local validSides = {top=true, bottom=true, left=true, right=true, front=true, back=true}
if not validSides[redstoneSide] then
  say("redstone_side must be top, bottom, left, right, front, or back.")
  return
end
-- De-energize the configured relay before validating secrets or other settings.
failClosed(redstoneSide)
if not terminalToken:match("^mbclub_") or #terminalToken < 40 then
  say("club_token is missing or invalid. Provision a terminal with the MineBank operator tool.")
  return
end
if pulseSeconds < 1 or pulseSeconds > 5 then
  say("door_pulse_seconds must be between 1 and 5 seconds.")
  return
end
if #terminalName < 1 or #terminalName > 60 then
  say("terminal_name must be 1-60 characters.")
  return
end

local journal = readJson(JOURNAL) or {}
if type(journal.counter) ~= "number" then journal.counter = 0 end

local function newKey()
  journal.counter = journal.counter + 1
  local computerId = os.getComputerID and os.getComputerID() or 0
  local now = os.epoch and os.epoch("utc") or math.floor(os.clock() * 1000)
  local key = ("mineclubs-%d-%d-%d"):format(computerId, now, journal.counter)
  journal.pending_key = key
  journal.pending_since = now
  writeJson(JOURNAL, journal)
  return key
end

local function promptNonEmpty(label)
  while true do
    write(label)
    local value = read()
    if value and value:match("%S") then return value end
    say("A value is required.")
  end
end

local function apiAdmission(cardCredential, pin, key)
  local payload = textutils.serializeJSON({wallet_pin=pin})
  local headers = {
    ["Content-Type"] = "application/json",
    ["Authorization"] = "Bearer " .. cardCredential,
    ["X-MineBank-Club-Token"] = terminalToken,
    ["Idempotency-Key"] = key,
  }
  local response, err = http.post(apiBase .. API_PATH, payload, headers)
  if not response then
    return nil, "No authoritative response: " .. tostring(err or "network error"), nil
  end
  local status = response.getResponseCode()
  local body = response.readAll()
  response.close()
  local ok, data = pcall(textutils.unserializeJSON, body)
  if not ok or type(data) ~= "table" then
    return nil, "The server response was not valid JSON. Do not create a new attempt.", status
  end
  if status >= 200 and status < 300 and data.status == "ADMITTED" then
    return data, nil, status
  end
  return nil, tostring(data.error or ("Admission rejected (HTTP " .. tostring(status) .. ").")), status
end

local function openDoor()
  failClosed(redstoneSide)
  redstone.setOutput(redstoneSide, true)
  os.sleep(pulseSeconds)
  redstone.setOutput(redstoneSide, false)
end

local function clearPending()
  journal.pending_key = nil
  journal.pending_since = nil
  writeJson(JOURNAL, journal)
end

local function runAdmission()
  term.clear()
  term.setCursorPos(1,1)
  print("MineBank MineClubs")
  print("------------------")
  local pending = journal.pending_key
  local key
  if pending then
    print("An earlier attempt has an uncertain result.")
    print("The same idempotency key will be reused; this prevents duplicate charges.")
    print("Type RESUME to retry safely, or CANCEL to return.")
    local choice = read()
    if choice ~= "RESUME" then return end
    key = pending
  else
    key = newKey()
  end

  local card = promptNonEmpty("MineBank card credential: ")
  write("Wallet PIN: ")
  local pin = read("*")
  if not pin or not pin:match("^%d%d%d%d%d%d$") then
    say("Wallet PIN must contain exactly six digits.")
    return
  end

  say("Contacting MineBank securely...")
  local data, err, status = apiAdmission(card, pin, key)
  pin = nil
  card = nil

  if data then
    clearPending()
    say("ADMISSION APPROVED")
    print(tostring(data.club_name or "MineClubs"))
    print("Fee: " .. tostring(data.fee or 0) .. " Emeralds")
    if data.unlimited_admission then print("Unlimited-admission tier") end
    print("Reference: " .. tostring(data.admission_id or "recorded"))
    say("Opening entrance...")
    openDoor()
    say("Entry authorized. Please proceed.")
    return
  end

  failClosed(redstoneSide)
  say("ADMISSION NOT CONFIRMED")
  print(err or "Unknown error")
  if status and status >= 400 and status < 500 then
    -- A definitive 4xx response means no admission was authorized. A new attempt
    -- may be made after the customer fixes the issue (e.g. Balance or PIN).
    clearPending()
  else
    print("Pending reference retained: " .. tostring(key))
    print("Resolve network/server uncertainty with RESUME; do not start a new attempt.")
  end
end

while true do
  failClosed(redstoneSide)
  term.clear()
  term.setCursorPos(1,1)
  print("MineBank MineClubs")
  print(terminalName)
  print("")
  print("1. Authorize admission")
  print("2. Exit")
  write("> ")
  local choice = read()
  if choice == "1" then
    local ok, err = pcall(runAdmission)
    failClosed(redstoneSide)
    if not ok then say("Controller error: " .. tostring(err)) end
    say("Press Enter to continue.")
    read()
  elseif choice == "2" then
    failClosed(redstoneSide)
    break
  else
    say("Choose 1 or 2.")
    os.sleep(1)
  end
end
