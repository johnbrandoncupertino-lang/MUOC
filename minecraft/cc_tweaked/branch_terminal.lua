-- MineBank branch terminal for CC:Tweaked.
-- Configure API_BASE to the HTTPS origin of your MineBank deployment.
-- Passwords and Wallet PINs stay in RAM only and are never written to disk.

local API_BASE = "https://minebanksecurearea.vercel.app/api/v1/minecraft"
local CARD_FILE = "minebank.card"
local runningProgram = shell.getRunningProgram()
local programDir = fs.getDir(runningProgram)
local uiPath = fs.combine(programDir, "minebank_ui.lua")
local ui = dofile(uiPath)
if not ui.hasBasalt() then ui.showMissingBasalt(); return end
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
ui.API_BASE = API_BASE

local function pause() ui.pause() end

local function secret(prompt)
  return ui.secret(prompt)
end

local function requestJson(url, body, headers)
  local response, err = http.post(
    url,
    textutils.serializeJSON(body),
    headers or { ["Content-Type"] = "application/json" }
  )
  if not response then
    return nil, tostring(err or "The server could not be reached.")
  end

  local status = response.getResponseCode and response.getResponseCode() or 200
  local raw = response.readAll()
  response.close()

  local ok, decoded = pcall(textutils.unserializeJSON, raw)
  if not ok or type(decoded) ~= "table" then
    return nil, "MineBank returned an invalid response (HTTP " .. tostring(status) .. ")."
  end
  if status < 200 or status >= 300 then
    return nil, tostring(decoded.error or ("MineBank returned HTTP " .. tostring(status) .. "."))
  end
  return decoded, nil
end

local function getJson(url, token)
  local response, err = http.get(url, {
    ["Authorization"] = "Bearer " .. token,
    ["Accept"] = "application/json"
  })
  if not response then
    return nil, tostring(err or "The server could not be reached.")
  end

  local status = response.getResponseCode and response.getResponseCode() or 200
  local raw = response.readAll()
  response.close()

  local ok, decoded = pcall(textutils.unserializeJSON, raw)
  if not ok or type(decoded) ~= "table" then
    return nil, "MineBank returned an invalid response (HTTP " .. tostring(status) .. ")."
  end
  if status < 200 or status >= 300 then
    return nil, tostring(decoded.error or ("MineBank returned HTTP " .. tostring(status) .. "."))
  end
  return decoded, nil
end

local function getDriveAndMount()
  local drive = peripheral.find("drive")
  if not drive then
    return nil, nil, "Connect a floppy disk drive to this terminal."
  end
  if not drive.isDiskPresent or not drive.isDiskPresent() then
    return nil, nil, "Insert a blank floppy disk before issuing a card."
  end
  local mount = drive.getMountPath and drive.getMountPath()
  if not mount or not fs.exists(mount) then
    return nil, nil, "The inserted disk is not mounted or does not contain data storage."
  end
  if fs.exists(fs.combine(mount, CARD_FILE)) then
    return nil, nil, "This floppy already contains a MineBank card file. Use a blank disk."
  end
  return drive, mount, nil
end

local action = ui.choose("BANK ASSISTANT", {
  "Link / issue a MineBank floppy card",
  "Frequently asked questions",
  "Exit"
}, "Touch an option. Passwords and PINs are never stored on the floppy.")
if action == 2 then
  ui.clear(); ui.header("BANK ASSISTANT", "Frequently asked questions")
  print("Cards: your floppy stores a revocable card credential, not your password or PIN.")
  print("Lost card: use the ATM self-service revoke flow or revoke it in the portal.")
  print("Balance: use the ATM or card reader for a live server check.")
  print("Payments: insert the card only when the trusted merchant terminal asks.")
  print("Security: never share your floppy, Wallet PIN or MineBank password.")
  print("Advanced transfers, account settings and credit products remain on the portal.")
  pause(); return
elseif action ~= 1 then
  return
end
print("")
print("Your password and Wallet PIN are used only for this request and are not stored.")

if not API_BASE:match("^https://") then
  print("SECURITY ERROR: API_BASE must use HTTPS.")
  pause()
  return
end

if API_BASE:find("YOUR%-MINEBANK%-DOMAIN") then
  print("SETUP REQUIRED: edit API_BASE in this program first.")
  pause()
  return
end

if not http then
  print("HTTP API is unavailable. Enable the CC:Tweaked HTTP setting.")
  pause()
  return
end

local drive, mount, driveError = getDriveAndMount()
if driveError then
  print(driveError)
  pause()
  return
end

local email = ui.prompt("MineBank email")
local password = secret("MineBank password: ")
local pin = secret("Wallet PIN (6 digits): ")
print("")

if not email or email == "" or not password or password == "" or not pin or #pin ~= 6 or not pin:match("^%d%d%d%d%d%d$") then
  print("Please provide an email, password, and six-digit Wallet PIN.")
  pause()
  return
end

print("")
print("Authenticating with MineBank...")
local accountResponse, accountError = requestJson(API_BASE .. "/accounts", {
  email = email,
  password = password,
  wallet_pin = pin
})
if not accountResponse then
  print("Could not load accounts: " .. accountError)
  pause()
  return
end

local accounts = accountResponse.accounts or {}
if #accounts == 0 then
  print("No eligible active accounts were found.")
  pause()
  return
end

local accountOptions={}
for i,account in ipairs(accounts) do
  accountOptions[#accountOptions+1]=string.format("%s | %s | %s | %s Emeralds",
    tostring(account.account_type),tostring(account.tier),tostring(account.account_number),tostring(account.balance))
end
local choice=ui.choose("SELECT ACCOUNT",accountOptions,"Choose the account to link to this floppy card.")
if not choice or not accounts[choice] then print("Cancelled. No card was issued."); pause(); return end
local selected=accounts[choice]

local confirmIssue=ui.choose("ISSUE CARD",{"Issue card","Cancel"},"Account "..tostring(selected.account_number)..". The floppy will contain a revocable card credential.")
if confirmIssue~=1 then ui.message("Cancelled. No card was issued.",true); pause(); return end

print("")
print("Issuing card...")
local issued, issueError = requestJson(API_BASE .. "/cards", {
  email = email,
  password = password,
  wallet_pin = pin,
  account_id = selected.id,
  label = "Minecraft card"
})
if not issued then
  print("Card issuance failed: " .. issueError)
  pause()
  return
end

local card = {
  version = 1,
  credential = issued.credential,
  card_id = issued.card and issued.card.id,
  account_number = issued.card and issued.card.account_number,
  label = issued.card and issued.card.label or "Minecraft card"
}
local path = fs.combine(mount, CARD_FILE)
local handle = fs.open(path, "w")
if not handle then
  print("The card was issued, but the floppy could not be written.")
  print("Attempting to revoke the new card for safety...")
  local revoked, revokeError = requestJson(API_BASE .. "/cards/revoke", {
    email = email,
    password = password,
    wallet_pin = pin,
    card_id = card.card_id
  })
  if revoked then
    print("The card was revoked. Please try again with a blank floppy.")
  else
    print("Automatic revocation failed: " .. tostring(revokeError))
    print("Contact MineBank support and provide card ID " .. tostring(card.card_id) .. ".")
  end
  pause()
  return
end

local writeOk, writeError = pcall(function()
  handle.write(textutils.serializeJSON(card))
  handle.close()
end)
if not writeOk then
  pcall(function() handle.close() end)
  fs.delete(path)
  print("The floppy write failed. Attempting to revoke the card...")
  local revoked, revokeError = requestJson(API_BASE .. "/cards/revoke", {
    email = email,
    password = password,
    wallet_pin = pin,
    card_id = card.card_id
  })
  if revoked then
    print("The card was revoked. Please retry with a working floppy.")
  else
    print("Automatic revocation failed: " .. tostring(revokeError))
    print("Contact MineBank support and provide card ID " .. tostring(card.card_id) .. ".")
  end
  pause()
  return
end

if drive.setDiskLabel then
  pcall(function() drive.setDiskLabel("MineBank Card") end)
end

print("")
print("CARD ISSUED")
print("Account: " .. tostring(card.account_number))
print("The card credential is stored on the floppy only.")
print("Keep the floppy secure. Revoke it if lost.")
pause()
