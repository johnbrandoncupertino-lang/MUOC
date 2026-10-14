-- MineBank merchant checkout terminal for CC:Tweaked.
-- Run only on a trusted shop computer controlled by the merchant.
-- Keep MERCHANT_TOKEN secret; never use this program on a player-accessible computer.

local API_BASE = "https://minebanksecurearea.vercel.app/api/v1/minecraft"
local MERCHANT_TOKEN = "SET_MERCHANT_TOKEN_HERE"
local CARD_FILE = "minebank.card"
local runningProgram = shell.getRunningProgram()
local programDir = fs.getDir(runningProgram)
local uiPath = fs.combine(programDir, "minebank_ui.lua")
local ui = dofile(uiPath)
ui.API_BASE = API_BASE

local function pause() ui.pause() end
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
  return ui.api(path, "POST", token, body, key)
end

ui.clear(); ui.header("MERCHANT CHECKOUT","Fixed-amount customer-authorized payment")
if not API_BASE:match("^https://") or API_BASE:find("YOUR%-MINEBANK%-DOMAIN") then print("Configure the HTTPS API_BASE."); pause(); return end
if MERCHANT_TOKEN == "SET_MERCHANT_TOKEN_HERE" or not MERCHANT_TOKEN:find("^mbmerchant_") then print("SETUP REQUIRED: configure a dedicated merchant token."); pause(); return end
if not http then print("Enable the CC:Tweaked HTTP API."); pause(); return end

local amountText = ui.prompt("Purchase amount in Emeralds")
local amount = tonumber(amountText)
if not amount or amount % 1 ~= 0 or amount < 1 or amount > 1000000 then print("Enter a whole Emerald amount from 1 to 1,000,000."); pause(); return end
amount = math.floor(amount)
local description = string.sub(ui.prompt("Short receipt description", "Minecraft shop purchase") or "Minecraft shop purchase", 1, 200)
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
print("Merchant: " .. tostring(checkout.merchant or "MineBank merchant"))
print("Exact amount: " .. tostring(checkout.amount) .. " Emeralds")
local confirmPayment=ui.choose("CONFIRM PAYMENT",{"Pay "..tostring(checkout.amount).." Emeralds","Cancel"},"Merchant: "..tostring(checkout.merchant or "MineBank merchant"))
if confirmPayment~=1 then print("Cancelled. No payment request sent."); pause(); return end
write("Customer Wallet PIN (6 digits): ")
local walletPin = read("*")
if not walletPin or not walletPin:match("^%d%d%d%d%d%d$") then print("PIN must contain six digits."); pause(); return end
local paymentKey = "checkout-" .. tostring(checkout.checkout_id)
print("Contacting MineBank...")
local paid, payError = post("/payments", card.credential, {checkout_id=tostring(checkout.checkout_id), wallet_pin=walletPin}, paymentKey)
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
