-- MineBank all-in-one installer for CC:Tweaked.
-- Run: pastebin run <id> (or download this file as minebank-install.lua), then run it.
-- Downloads only public program code. It never asks for or stores banking credentials.

local BASE = "https://raw.githubusercontent.com/johnbrandoncupertino-lang/MUOC/main/minecraft/cc_tweaked/"
local TARGET = "minebank"
local files = {
  {name="minebank_ui.lua", url=BASE.."minebank_ui.lua", label="Shared MineBank utilities"},
  {name="atm.lua", url=BASE.."atm.lua", label="ATM"},
  {name="branch_terminal.lua", url=BASE.."branch_terminal.lua", label="Bank Assistant"},
  {name="merchant_checkout.lua", url=BASE.."merchant_checkout.lua", label="Merchant Checkout"},
  {name="vending_machine.lua", url=BASE.."vending_machine.lua", label="Automated Vending"},
  {name="README.md", url=BASE.."README.md", label="Setup guide"},
}

local function say(text, color)
  if term and colors then term.setTextColor(color or colors.white) end
  print(text)
  if term and colors then term.setTextColor(colors.white) end
end

local function fetch(url)
  local ok, response, err = pcall(http.get, url, nil, true)
  if not ok or not response then return nil, tostring(err or response or "HTTP request failed") end
  local status = response.getResponseCode and response.getResponseCode() or 200
  local body = response.readAll()
  response.close()
  if status < 200 or status >= 300 then return nil, "HTTP " .. tostring(status) end
  if type(body) ~= "string" or #body < 20 then return nil, "Downloaded file is empty or incomplete" end
  return body
end

if not http then
  say("ERROR: Enable the CC:Tweaked HTTP API first.", colors.red)
  return
end

term.setBackgroundColor(colors.black)
term.setTextColor(colors.lime)
term.clear()
term.setCursorPos(1, 1)
say("MINEBANK  /  THE NEW BANK", colors.lime)
say("All-in-one installer", colors.white)
say("")
say("This installs the ATM, Bank Assistant, Merchant Checkout,")
say("Vending Machine and shared utilities with native UI.")
say("No third-party UI framework is required.")
say("No passwords, Wallet PINs or machine tokens are requested.")
say("")
write("Install/update into /" .. TARGET .. "? Type INSTALL to continue: ")
if read() ~= "INSTALL" then say("Cancelled. No files changed.", colors.yellow); return end

if not fs.exists(TARGET) then fs.makeDir(TARGET) end
local tempDir = fs.combine(TARGET, ".install_tmp")
if fs.exists(tempDir) then fs.delete(tempDir) end
fs.makeDir(tempDir)

local downloaded = {}
for i, file in ipairs(files) do
  write("[" .. i .. "/" .. #files .. "] " .. file.label .. "... ")
  local body, err = fetch(file.url)
  if not body then
    say("FAILED", colors.red)
    say("Could not download " .. file.name .. ": " .. tostring(err), colors.red)
    fs.delete(tempDir)
    say("Existing installation left unchanged.", colors.yellow)
    return
  end

  -- Basalt2's full bundle may be run in an environment without native
  -- require(), and some releases pass module paths with a trailing .lua.
  -- Normalize those paths before consulting the embedded module registry.

  local path = fs.combine(tempDir, file.name)
  local handle = fs.open(path, "w")
  if not handle then
    say("FAILED", colors.red)
    fs.delete(tempDir)
    say("Cannot write to " .. path .. ".", colors.red)
    return
  end
  handle.write(body)
  handle.close()
  downloaded[#downloaded + 1] = file.name
  say("OK", colors.lime)
end

-- Only replace live files after every download has succeeded.
local backupDir = fs.combine(TARGET, ".install_backup")
if fs.exists(backupDir) then fs.delete(backupDir) end
fs.makeDir(backupDir)
for _, name in ipairs(downloaded) do
  local live = fs.combine(TARGET, name)
  if fs.exists(live) then
    fs.move(live, fs.combine(backupDir, name))
  end
end

local installed = 0
for _, name in ipairs(downloaded) do
  local ok = pcall(fs.move, fs.combine(tempDir, name), fs.combine(TARGET, name))
  if not ok then
    say("Install failed while replacing " .. name .. ". Restoring previous files.", colors.red)
    for _, restoreName in ipairs(downloaded) do
      local live = fs.combine(TARGET, restoreName)
      local backup = fs.combine(backupDir, restoreName)
      if fs.exists(live) then fs.delete(live) end
      if fs.exists(backup) then pcall(fs.move, backup, live) end
    end
    fs.delete(tempDir)
    fs.delete(backupDir)
    return
  end
  installed = installed + 1
end

fs.delete(tempDir)
fs.delete(backupDir)
-- Balance is now an ATM service, not a separate installed program.
local oldBalanceReader=fs.combine(TARGET,"balance_reader.lua")
if fs.exists(oldBalanceReader) then fs.delete(oldBalanceReader) end
say("")
say("INSTALLATION COMPLETE", colors.lime)
say(installed .. " files installed in /" .. TARGET .. ".", colors.white)
say("Next steps:", colors.white)
say("1. Run: cd " .. TARGET, colors.white)
say("2. Run: atm.lua (then complete the protected operator setup).", colors.white)
say("3. Run: branch_terminal.lua to issue a floppy card.", colors.white)
say("4. Configure merchant credentials only on locked-down shop computers.", colors.white)
say("5. Keep transaction features disabled on the server until staging tests pass.", colors.yellow)
say("")
say("Native CC:Tweaked UI installed; Basalt is not required.", colors.gray)
