-- MineBank shared CC:Tweaked terminal utilities.
-- Save beside the MineBank programs as minebank_ui.lua.
local M = {}
-- Use an attached monitor as the primary display when available. Keyboard input
-- still comes from the computer, while terminal output is redirected to the monitor.
if peripheral and peripheral.find and term and term.redirect then
  local monitor = peripheral.find("monitor")
  if monitor then
    pcall(function() monitor.setTextScale(0.5) end)
    pcall(function() term.redirect(monitor) end)
  end
end
M.API_BASE = "https://minebanksecurearea.vercel.app/api/v1/minecraft"
M.CARD_FILE = "minebank.card"

function M.clear()
  term.setBackgroundColor(colors.black)
  term.setTextColor(colors.white)
  term.clear()
  term.setCursorPos(1,1)
end

function M.header(title, subtitle)
  local w,h = term.getSize()
  term.setBackgroundColor(colors.black); term.setTextColor(colors.lime)
  term.setCursorPos(1,1); term.write("MINEBANK")
  term.setTextColor(colors.lightGray); term.setCursorPos(1,2)
  term.write("THE NEW BANK  /  "..tostring(title or "TERMINAL"))
  term.setTextColor(colors.gray); term.setCursorPos(1,3); term.write(string.rep("-", math.max(1,w)))
  if subtitle then term.setCursorPos(1,4); term.setTextColor(colors.white); term.write(tostring(subtitle):sub(1,w)) end
  term.setBackgroundColor(colors.black); term.setTextColor(colors.white)
end

function M.message(text, good)
  local w = term.getSize()
  term.setTextColor(good and colors.lime or colors.orange)
  print(tostring(text):sub(1, math.max(1,w)))
  term.setTextColor(colors.white)
end

function M.pause()
  print(""); write("Press Enter to continue. "); read()
end

function M.secret(label)
  write(label)
  return read("*")
end

function M.prompt(label, default)
  if default then write(label.." ["..tostring(default).."]: ") else write(label..": ") end
  local v = read()
  if v == "" and default ~= nil then return tostring(default) end
  return v
end

function M.api(path, method, token, body, key)
  if not http then return nil, "Enable the CC:Tweaked HTTP API in server settings." end
  local headers = {Accept="application/json", ["Content-Type"]="application/json"}
  if token then headers.Authorization = "Bearer "..token end
  if key then headers["Idempotency-Key"] = key end
  local opts = {url=M.API_BASE..path, method=method or (body and "POST" or "GET"), headers=headers, timeout=15}
  if body then opts.body = textutils.serializeJSON(body) end
  local ok, response, err = pcall(http.request, opts)
  if not ok then return nil, "Network request failed: "..tostring(response) end
  if not response then return nil, tostring(err or "MineBank could not be reached.") end
  local status = response.getResponseCode and response.getResponseCode() or 200
  local raw = response.readAll()
  response.close()
  local parsed, data = pcall(textutils.unserializeJSON, raw)
  if not parsed or type(data) ~= "table" then return nil, "Invalid MineBank response (HTTP "..status..")." end
  if status < 200 or status >= 300 then return nil, tostring(data.error or ("HTTP "..status)) end
  return data
end

function M.card()
  local drive = peripheral.find("drive")
  if not drive or not drive.isDiskPresent or not drive.isDiskPresent() then return nil, "Insert your MineBank floppy card." end
  local mount = drive.getMountPath and drive.getMountPath()
  if not mount or not fs.exists(mount) then return nil, "The floppy is not mounted." end
  local path = fs.combine(mount, M.CARD_FILE)
  if not fs.exists(path) then return nil, "No MineBank card file found on this floppy." end
  local h = fs.open(path,"r")
  if not h then return nil, "Cannot read the floppy card." end
  local raw = h.readAll(); h.close()
  local ok, card = pcall(textutils.unserializeJSON,raw)
  if not ok or type(card)~="table" or type(card.credential)~="string" or not card.credential:match("^mbmc_") then
    return nil, "Invalid MineBank floppy card."
  end
  return card
end

function M.key(prefix)
  return tostring(prefix or "mb").."-"..tostring(os.epoch("utc")).."-"..tostring(os.getComputerID()).."-"..tostring(math.random(100000,999999))
end

function M.inventory(name)
  if not name or name == "" then return nil end
  local ok, inv = pcall(peripheral.wrap, name)
  if not ok or not inv or not inv.list or not inv.pushItems then return nil end
  return inv
end

function M.findInventory()
  local names = peripheral.getNames()
  local found = {}
  for _,name in ipairs(names) do
    local inv = M.inventory(name)
    if inv then found[#found+1] = name end
  end
  return found
end

function M.choose(title, options)
  M.clear(); M.header(title, "Use number keys or type the option number.")
  for i,option in ipairs(options) do print(string.format("%d) %s", i, option)) end
  write("> ")
  local n = tonumber(read())
  if n and n % 1 == 0 and options[n] then return n end
  return nil
end

function M.setup()
  M.clear(); M.header("SETUP WIZARD", "MineBank terminal configuration")
  print("This wizard only saves local terminal configuration.")
  print("It does not change your MineBank account or portal.")
  local base = M.prompt("HTTPS API base", M.API_BASE)
  if not base:match("^https://") then return nil, "API base must use HTTPS." end
  M.API_BASE = base:gsub("/+$","")
  return {api_base=M.API_BASE}
end

return M
