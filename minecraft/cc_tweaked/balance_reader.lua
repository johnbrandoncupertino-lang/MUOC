-- MineBank card reader for CC:Tweaked.
-- Configure API_BASE to the HTTPS origin of your MineBank deployment.
-- The floppy contains a revocable card credential only, never a password or PIN.

local API_BASE = "https://muoc-pietroaccordi09-4057.vercel.app/api/v1/minecraft"
local CARD_FILE = "minebank.card"

local function pause()
  print("")
  print("Press Enter to continue.")
  read()
end

local function loadCard()
  local drive = peripheral.find("drive")
  if not drive or not drive.isDiskPresent or not drive.isDiskPresent() then
    return nil, "Insert a MineBank card floppy."
  end
  local mount = drive.getMountPath and drive.getMountPath()
  if not mount or not fs.exists(mount) then
    return nil, "The floppy is not mounted or has no readable data."
  end
  local path = fs.combine(mount, CARD_FILE)
  if not fs.exists(path) then
    return nil, "No MineBank card file was found on this floppy."
  end
  local handle = fs.open(path, "r")
  if not handle then
    return nil, "The MineBank card file could not be opened."
  end
  local raw = handle.readAll()
  handle.close()
  local ok, card = pcall(textutils.unserializeJSON, raw)
  if not ok or type(card) ~= "table" or type(card.credential) ~= "string" or not card.credential:find("^mbmc_") then
    return nil, "This floppy does not contain a valid MineBank card."
  end
  return card, nil
end

term.clear()
term.setCursorPos(1, 1)
print("MineBank | Account reader")
print("The New Bank")
print("------------------------------")

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

local card, cardError = loadCard()
if not card then
  print(cardError)
  pause()
  return
end

print("")
print("Contacting MineBank securely...")
local response, err = http.get(API_BASE .. "/account", {
  ["Authorization"] = "Bearer " .. card.credential,
  ["Accept"] = "application/json"
})
if not response then
  print("MineBank could not be reached: " .. tostring(err or "unknown error"))
  pause()
  return
end

local status = response.getResponseCode and response.getResponseCode() or 200
local raw = response.readAll()
response.close()
local ok, decoded = pcall(textutils.unserializeJSON, raw)
if not ok or type(decoded) ~= "table" then
  print("MineBank returned an invalid response (HTTP " .. tostring(status) .. ").")
  pause()
  return
end
if status < 200 or status >= 300 then
  print("MineBank rejected the card: " .. tostring(decoded.error or ("HTTP " .. status)))
  pause()
  return
end

local account = decoded.account or {}
print("")
print("ACCOUNT VERIFIED")
print("Number: " .. tostring(account.account_number))
print("Type: " .. tostring(account.account_type))
print("Tier: " .. tostring(account.tier))
print("Status: " .. tostring(account.status))
print("")
print("Available Balance")
print(tostring(account.balance) .. " Emeralds")
print("")
print("Shop payments: merchant_checkout.lua")
print("ATM withdrawals: atm.lua (run on a turtle)")
pause()
