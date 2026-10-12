-- MineBank branch terminal for CC:Tweaked.
-- Configure API_BASE to the HTTPS origin of your MineBank deployment.
-- Passwords and Wallet PINs stay in RAM only and are never written to disk.

local API_BASE = "https://vercel.com/pietro-apple/minebanksecurearea/2jLj1Ba2PB3kZrHzHNPx8aR6LNUH/api/v1/minecraft"
local CARD_FILE = "minebank.card"

local function pause()
  print("")
  print("Press Enter to continue.")
  read()
end

local function secret(prompt)
  write(prompt)
  return read("*")
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

term.clear()
term.setCursorPos(1, 1)
print("MineBank | Card issuance")
print("The New Bank")
print("------------------------------")
print("This terminal requires an HTTPS MineBank API.")
print("Your password and Wallet PIN will not be stored.")
print("")

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

write("MineBank email: ")
local email = read()
local password = secret("MineBank password: ")
print("")
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

print("")
print("Choose the account to link to this card:")
for i, account in ipairs(accounts) do
  print(string.format(
    "%d) %s | %s | %s | Balance: %s Emeralds",
    i,
    tostring(account.account_type),
    tostring(account.tier),
    tostring(account.account_number),
    tostring(account.balance)
  ))
end
print("")
write("Account number in list: ")
local choice = tonumber(read())
if not choice or choice < 1 or choice > #accounts or choice % 1 ~= 0 then
  print("Invalid selection.")
  pause()
  return
end
local selected = accounts[choice]

print("")
print("Issue a MineBank card for account " .. tostring(selected.account_number) .. "?")
write("Type ISSUE to confirm: ")
if read() ~= "ISSUE" then
  print("Cancelled. No card was issued.")
  pause()
  return
end

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
