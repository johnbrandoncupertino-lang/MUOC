-- MineBank merchant checkout terminal for CC:Tweaked.
-- Run only on a trusted shop computer controlled by the merchant.
-- Keep MERCHANT_TOKEN secret; never use this program on a player-accessible computer.

local API_BASE = "https://muoc-pietroaccordi09-4057.vercel.app/api/v1/minecraft"
local MERCHANT_TOKEN = "SET_MERCHANT_TOKEN_HERE"
local CARD_FILE = "minebank.card"

local function pause() print(""); print("Press Enter to continue."); read() end
local function readCard()
  local drive = peripheral.find("drive")
  if not drive or not drive.isDiskPresent or not drive.isDiskPresent() then return nil, "Insert the customer's MineBank card floppy." end
  local mount = drive.getMountPath and drive.getMountPath()
  if not mount or not fs.exists(mount) then return nil, "The floppy is not mounted." end
  local path = fs.combine(mount, CARD_FILE)
  if not fs.exists(path) then return nil, "No MineBank card file was found." end
  local h = fs.open(path, "r"); if not h then return nil, "Cannot read card file." end
  local raw = h.readAll(); h.close()
  local ok, card = pcall(textutils.unserializeJSON, raw)
  if not ok or type(card) ~= "table" or type(card.credential) ~= "string" or not card.credential:find("^mbmc_") then
    return nil, "Invalid MineBank card file."
  end
  return card
end

local function post(path, token, body, key)
  local headers = { ["Accept"]="application/json", ["Content-Type"]="application/json", ["Authorization"]="Bearer " .. token }
  if key then headers["Idempotency-Key"] = key end
  local response, err = http.post(API_BASE .. path, textutils.serializeJSON(body), headers)
  if not response then return nil, tostring(err or "network error") end
  local status = response.getResponseCode and response.getResponseCode() or 200
  local raw = response.readAll(); response.close()
  local ok, data = pcall(textutils.unserializeJSON, raw)
  if not ok or type(data) ~= "table" then return nil, "Invalid MineBank response (HTTP " .. status .. ")." end
  if status < 200 or status >= 300 then return nil, tostring(data.error or ("HTTP " .. status)) end
  return data
end

term.clear(); term.setCursorPos(1,1)
print("MineBank | Merchant checkout")
print("The New Bank")
print("------------------------------")
if not API_BASE:match("^https://") or API_BASE:find("YOUR%-MINEBANK%-DOMAIN") then print("Configure the HTTPS API_BASE."); pause(); return end
if MERCHANT_TOKEN == "SET_MERCHANT_TOKEN_HERE" or not MERCHANT_TOKEN:find("^mbmerchant_") then print("SETUP REQUIRED: configure a dedicated merchant token."); pause(); return end
if not http then print("Enable the CC:Tweaked HTTP API."); pause(); return end

write("Purchase amount in Emeralds: ")
local amountText = read()
local amount = tonumber(amountText)
if not amount or amount % 1 ~= 0 or amount < 1 or amount > 1000000 then print("Enter a whole Emerald amount from 1 to 1,000,000."); pause(); return end
write("Short receipt description: ")
local description = string.sub(read() or "Minecraft shop purchase", 1, 200)
local key = tostring(os.epoch("utc")) .. "-" .. tostring(os.getComputerID()) .. "-" .. tostring(math.random(100000,999999))
print("")
print("Creating checkout for " .. amount .. " Emeralds...")
local checkout, err = post("/merchant/checkouts", MERCHANT_TOKEN, {amount=amount, description=description}, key)
if not checkout then print("Checkout failed: " .. err); pause(); return end
print("Checkout: " .. tostring(checkout.checkout_id))
print("Amount fixed by MineBank: " .. tostring(checkout.amount) .. " Emeralds")
print("Expires: " .. tostring(checkout.expires_at))
print("")
print("Ask the customer to insert their MineBank card floppy.")
local card, cardError = readCard()
if not card then print(cardError); pause(); return end
write("Customer confirms payment? Type PAY: ")
if read() ~= "PAY" then print("Cancelled. No payment request sent."); pause(); return end
local paymentKey = "checkout-" .. tostring(checkout.checkout_id)
print("Contacting MineBank...")
local paid, payError = post("/payments", card.credential, {checkout_id=tostring(checkout.checkout_id)}, paymentKey)
if not paid then
  print("Payment not confirmed: " .. payError)
  print("Check MineBank transaction history before retrying.")
  pause(); return
end
print("")
print("PAYMENT COMPLETE")
print("Transaction: " .. tostring(paid.transaction_id))
print("Paid: " .. tostring(paid.amount) .. " Emeralds")
print("Checkout: " .. tostring(paid.checkout_id))
pause()
