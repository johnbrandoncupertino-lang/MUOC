-- MineBank native CC:Tweaked UI.
-- No Basalt, package library, third-party framework, or downloaded UI bundle is required.
-- Uses the standard terminal API and monitor_touch events so the UI works on stock CC:Tweaked.
local M = {}
M.API_BASE = "https://minebanksecurearea.vercel.app/api/v1/minecraft"
M.CARD_FILE = "minebank.card"

local nativeTerm = (term.native and term.native()) or term.current()
local target = "terminal"
local title = "MINEBANK"
local subtitle = "THE NEW BANK"
local messages = {}
local maxMessages = 120

local function safeText(value)
  local s = tostring(value == nil and "" or value)
  s = s:gsub("[\r\n]+", " ")
  return s
end

local function getMonitor()
  if not peripheral or type(peripheral.find) ~= "function" then return nil end
  local ok, value = pcall(peripheral.find, "monitor")
  if ok then return value end
  return nil
end

local function displayFor(name)
  if name == "monitor" then
    local d = getMonitor()
    if d then
      pcall(function() d.setTextScale(0.5) end)
      return d
    end
  end
  return nativeTerm
end

local function displaySize(d)
  local w, h = 51, 19
  pcall(function() w, h = d.getSize() end)
  return math.max(12, w or 51), math.max(8, h or 19)
end

local function withDisplay(fn, name)
  local d = displayFor(name or target)
  if not d then return false, "Display unavailable." end
  local previous = term.current and term.current() or nativeTerm
  if term.redirect then pcall(term.redirect, d) end
  local ok, a, b, c = pcall(fn, d)
  if term.redirect then pcall(term.redirect, previous or nativeTerm) end
  if not ok then return false, tostring(a) end
  return true, a, b, c
end

local function fit(s, width)
  s = safeText(s)
  if #s > width then
    if width <= 3 then return s:sub(1, width) end
    return s:sub(1, width - 3) .. "..."
  end
  return s
end

local function wrapText(text, width)
  width = math.max(1, width)
  local result = {}
  text = safeText(text)
  if text == "" then return {""} end
  local line = ""
  for word in text:gmatch("%S+") do
    if #word > width then
      if line ~= "" then result[#result + 1] = line; line = "" end
      while #word > width do
        result[#result + 1] = word:sub(1, width)
        word = word:sub(width + 1)
      end
      if word ~= "" then line = word end
    elseif line == "" then
      line = word
    elseif #line + 1 + #word <= width then
      line = line .. " " .. word
    else
      result[#result + 1] = line
      line = word
    end
  end
  if line ~= "" then result[#result + 1] = line end
  if #result == 0 then result[1] = "" end
  return result
end

local function writeAt(d, x, y, text, fg, bg, width)
  local w, h = displaySize(d)
  if y < 1 or y > h then return end
  x = math.max(1, math.min(w, x))
  local maxWidth = width or (w - x + 1)
  maxWidth = math.max(0, math.min(maxWidth, w - x + 1))
  pcall(function()
    d.setCursorPos(x, y)
    if bg then d.setBackgroundColor(bg) end
    if fg then d.setTextColor(fg) end
    d.write(fit(text, maxWidth))
  end)
end

local function drawHeader(d, pageTitle, pageSubtitle)
  local w, h = displaySize(d)
  pcall(function()
    d.setBackgroundColor(colors.black)
    d.setTextColor(colors.white)
    d.clear()
  end)
  writeAt(d, 2, 1, "MINEBANK  /  THE NEW BANK", colors.lime, colors.black, w - 3)
  writeAt(d, 2, 2, string.rep("=", math.max(1, w - 3)), colors.green, colors.black, w - 3)
  writeAt(d, 2, 3, string.upper(safeText(pageTitle or title)), colors.white, colors.black, w - 3)
  writeAt(d, 2, 4, safeText(pageSubtitle or subtitle), colors.lightGray, colors.black, w - 3)
  writeAt(d, 2, 5, string.rep("-", math.max(1, w - 3)), colors.green, colors.black, w - 3)
  writeAt(d, 2, h, "MINEBANK SECURE TERMINAL", colors.gray, colors.black, w - 3)
end

local function renderLog()
  withDisplay(function(d)
    local w, h = displaySize(d)
    drawHeader(d, title, subtitle)
    local rows = {}
    for _, item in ipairs(messages) do
      local wrapped = wrapText(item.text, w - 4)
      for _, line in ipairs(wrapped) do rows[#rows + 1] = {text=line, good=item.good} end
      rows[#rows + 1] = {text="", good=true}
    end
    local bodyRows = math.max(1, h - 7)
    local first = math.max(1, #rows - bodyRows + 1)
    local y = 6
    for i = first, #rows do
      if y >= h then break end
      local item = rows[i]
      writeAt(d, 2, y, item.text, item.good and colors.white or colors.red, colors.black, w - 3)
      y = y + 1
    end
    if #rows == 0 then
      writeAt(d, 2, 7, "Ready. Choose an operation to continue.", colors.lightGray, colors.black, w - 3)
    end
    writeAt(d, 2, h, "MINEBANK SECURE TERMINAL", colors.gray, colors.black, w - 3)
  end)
end

local function drawButton(d, x, y, width, height, label, bg, fg)
  local w, h = displaySize(d)
  x = math.max(1, x); y = math.max(1, y)
  width = math.max(1, math.min(width, w - x + 1))
  height = math.max(1, math.min(height, h - y + 1))
  local text = fit(label, width)
  local left = math.max(0, math.floor((width - #text) / 2))
  for row = 0, height - 1 do
    pcall(function()
      d.setCursorPos(x, y + row)
      d.setBackgroundColor(bg)
      d.setTextColor(fg)
      if row == math.floor((height - 1) / 2) then
        d.write(string.rep(" ", left) .. text .. string.rep(" ", math.max(0, width - left - #text)))
      else
        d.write(string.rep(" ", width))
      end
    end)
  end
end

local function readTerminal(prompt, default, secret)
  local previous = term.current and term.current() or nativeTerm
  if term.redirect then pcall(term.redirect, nativeTerm) end
  local _, h = displaySize(nativeTerm)
  pcall(function()
    nativeTerm.setBackgroundColor(colors.black)
    nativeTerm.setTextColor(colors.white)
    nativeTerm.setCursorPos(1, h)
    nativeTerm.clearLine()
    nativeTerm.write(fit(prompt, math.max(1, select(1, displaySize(nativeTerm)) - 1)) .. " ")
  end)
  local ok, value = pcall(read, secret and "*" or nil, nil, nil, default)
  if term.redirect then pcall(term.redirect, previous or nativeTerm) end
  if not ok then return nil end
  return value
end

local function monitorName()
  local d = getMonitor()
  if not d then return nil end
  if peripheral and type(peripheral.getName) == "function" then
    local ok, name = pcall(peripheral.getName, d)
    if ok then return name end
  end
  return nil
end

local function waitTouch()
  local wanted = monitorName()
  while true do
    local event, side, x, y = os.pullEventRaw()
    if event == "terminate" then return nil end
    if event == "monitor_touch" and type(x) == "number" and type(y) == "number" then
      if not wanted or side == wanted then return x, y end
    end
  end
end

local function isTouchTarget()
  return target == "monitor" and getMonitor() ~= nil
end

function M.hasBasalt()
  -- Backwards-compatible API for older app versions: the native renderer is ready.
  return true
end

function M.showMissingBasalt()
  M.message("This MineBank UI uses the built-in CC:Tweaked terminal; Basalt is not required.", true)
  return false
end

function M.useTarget(name)
  if name == "monitor" and getMonitor() then target = "monitor" else target = "terminal" end
  return target
end

function M.hasMonitor()
  return getMonitor() ~= nil
end

function M.clear()
  messages = {}
  title = "MINEBANK"
  subtitle = "THE NEW BANK"
  renderLog()
end

function M.header(newTitle, newSubtitle)
  title = safeText(newTitle or "MINEBANK")
  subtitle = safeText(newSubtitle or "")
  messages = {}
  renderLog()
end

function M.message(text, good)
  text = safeText(text)
  if text == "" then return true end
  messages[#messages + 1] = {text=text, good=(good ~= false)}
  while #messages > maxMessages do table.remove(messages, 1) end
  renderLog()
  return true
end

function M.pause()
  if isTouchTarget() then
    withDisplay(function(d)
      local w, h = displaySize(d)
      drawHeader(d, title, subtitle)
      local wrapped = {}
      for _, item in ipairs(messages) do
        for _, line in ipairs(wrapText(item.text, w - 4)) do wrapped[#wrapped + 1] = {text=line, good=item.good} end
      end
      local y = 7
      for i = math.max(1, #wrapped - (h - 10)), #wrapped do
        if y >= h - 3 then break end
        writeAt(d, 2, y, wrapped[i].text, wrapped[i].good and colors.white or colors.red, colors.black, w - 3)
        y = y + 1
      end
      drawButton(d, 2, h - 2, w - 3, 1, "TOUCH TO CONTINUE", colors.green, colors.black)
    end)
    local _, y = waitTouch()
    return y ~= nil
  end
  return readTerminal("Press Enter to continue", nil, false) ~= nil
end

function M.choose(menuTitle, options, menuSubtitle)
  options = options or {}
  if type(options) ~= "table" or #options == 0 then return nil end
  menuTitle = safeText(menuTitle or "SELECT")
  menuSubtitle = safeText(menuSubtitle or "Select an option to continue.")

  if isTouchTarget() then
    local page = 1
    while true do
      local _, w, h
      withDisplay(function(d)
        w, h = displaySize(d)
      end)
      local cols = math.max(1, math.min(3, math.floor((w - 2) / 12)))
      local cellW = math.max(1, math.floor((w - 2 - (cols - 1)) / cols))
      local rowsPerPage = math.max(1, math.floor((h - 10) / 3))
      local pageSize = cols * rowsPerPage
      local pages = math.max(1, math.ceil(#options / pageSize))
      if page > pages then page = pages end
      withDisplay(function(d)
        drawHeader(d, menuTitle, menuSubtitle)
        local first = (page - 1) * pageSize + 1
        local last = math.min(#options, first + pageSize - 1)
        for i = first, last do
          local offset = i - first
          local col = math.floor(offset / rowsPerPage)
          local row = offset % rowsPerPage
          local x = 2 + col * (cellW + 1)
          local y = 7 + row * 3
          local label = safeText(options[i])
          local lower = label:lower()
          local cancel = lower:find("cancel",1,true) or lower:find("back",1,true) or lower:find("exit",1,true)
          local danger = lower:find("revoke",1,true) or lower:find("delete",1,true)
          local bg = danger and colors.red or (cancel and colors.gray or colors.green)
          drawButton(d, x, y, cellW, 2, label, bg, cancel and colors.white or colors.black)
        end
        if pages > 1 then
          local half = math.max(1, math.floor((w - 4) / 2))
          if page > 1 then drawButton(d, 2, h - 2, half, 1, "PREVIOUS", colors.gray, colors.white) end
          if page < pages then drawButton(d, 3 + half, h - 2, half, 1, "NEXT", colors.gray, colors.white) end
        else
          writeAt(d, 2, h - 2, "TOUCH AN OPTION", colors.lightGray, colors.black, w - 3)
        end
      end)
      local x, y = waitTouch()
      if not x then return nil end
      if pages > 1 and y >= h - 2 then
        if page > 1 and x <= math.floor((w + 1) / 2) then page = page - 1
        elseif page < pages and x > math.floor((w + 1) / 2) then page = page + 1 end
      else
        local first = (page - 1) * pageSize + 1
        local last = math.min(#options, first + pageSize - 1)
        for i = first, last do
          local offset = i - first
          local col = math.floor(offset / rowsPerPage)
          local row = offset % rowsPerPage
          local x1 = 2 + col * (cellW + 1)
          local y1 = 7 + row * 3
          if x >= x1 and x < x1 + cellW and y >= y1 and y < y1 + 2 then return i end
        end
      end
    end
  end

  local page = 1
  local _, h = displaySize(nativeTerm)
  local pageSize = math.max(1, h - 10)
  local pages = math.max(1, math.ceil(#options / pageSize))
  while true do
    local first = (page - 1) * pageSize + 1
    local last = math.min(#options, first + pageSize - 1)
    local rows = {}
    for i = first, last do rows[#rows + 1] = {text=string.format("%2d  %s", i, safeText(options[i])), good=true} end
    withDisplay(function(d)
      drawHeader(d, menuTitle, menuSubtitle)
      local _, height = displaySize(d)
      local y = 7
      for _, item in ipairs(rows) do
        if y >= height - 2 then break end
        writeAt(d, 2, y, item.text, colors.lime, colors.black, select(1, displaySize(d)) - 3)
        y = y + 1
      end
      local foot = pages > 1 and ("Page " .. page .. "/" .. pages .. " | number to select, N/P to page, 0 to cancel") or "Type a number to select, or 0 to cancel."
      writeAt(d, 2, height - 2, foot, colors.lightGray, colors.black, select(1, displaySize(d)) - 3)
    end)
    local value = readTerminal("Selection", nil, false)
    if value == nil then return nil end
    local lowered = value:lower():match("^%s*(.-)%s*$")
    if lowered == "0" or lowered == "" or lowered == "q" or lowered == "cancel" then return nil end
    if pages > 1 and (lowered == "n" or lowered == "next") then page = math.min(pages, page + 1)
    elseif pages > 1 and (lowered == "p" or lowered == "prev" or lowered == "previous") then page = math.max(1, page - 1)
    else
      local selected = tonumber(lowered)
      if selected and selected % 1 == 0 and selected >= first and selected <= last then return selected end
      M.message("Invalid selection. Choose a listed number.", false)
    end
  end
end

local function touchInput(label, default, secret, numericOnly, isPin)
  local value = tostring(default or "")
  local w, h
  withDisplay(function(d)
    w, h = displaySize(d)
    drawHeader(d, secret and "SECURE INPUT" or "WORKSPACE INPUT", label)
    writeAt(d, 2, 6, (secret and "VALUE: " or "INPUT: ") .. (secret and string.rep("*", #value) or value), colors.lime, colors.black, w - 3)
  end)

  local function drawKeyboard(note)
    withDisplay(function(d)
      w, h = displaySize(d)
      drawHeader(d, secret and "SECURE INPUT" or "WORKSPACE INPUT", label)
      writeAt(d, 2, 6, (secret and "VALUE: " or "INPUT: ") .. (secret and string.rep("*", #value) or value), colors.lime, colors.black, w - 3)
      if note then writeAt(d, 2, 7, note, colors.yellow, colors.black, w - 3) end
      if numericOnly or isPin then
        local keys = {"1","2","3","4","5","6","7","8","9","CLEAR","0","DEL"}
        local cols = 3
        local cw = math.max(2, math.floor((w - 5) / cols))
        for i, key in ipairs(keys) do
          local row = math.floor((i - 1) / cols)
          local col = (i - 1) % cols
          drawButton(d, 2 + col * (cw + 1), 8 + row * 2, cw, 1, key, colors.gray, colors.white)
        end
        local half = math.max(1, math.floor((w - 5) / 2))
        drawButton(d, 2, h - 2, half, 1, "CANCEL", colors.red, colors.white)
        drawButton(d, 3 + half, h - 2, half, 1, isPin and "CONFIRM PIN" or "OK", colors.green, colors.black)
      else
        local rows = {"1234567890", "QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM-_"}
        local cw = math.max(1, math.floor((w - 2) / 10))
        for ri, row in ipairs(rows) do
          for ci = 1, #row do
            local key = row:sub(ci,ci)
            drawButton(d, 2 + (ci - 1) * cw, 7 + ri * 2, cw, 1, key, colors.gray, colors.white)
          end
        end
        local half = math.max(1, math.floor((w - 6) / 5))
        local acts = {"SPACE","BACK","CLEAR","CANCEL","OK"}
        for i, act in ipairs(acts) do
          local bg = act == "CANCEL" and colors.red or (act == "OK" and colors.green or colors.gray)
          drawButton(d, 2 + (i - 1) * (half + 1), h - 2, half, 1, act, bg, colors.white)
        end
      end
    end)
  end

  drawKeyboard()
  while true do
    local x, y = waitTouch()
    if not x then return nil end
    w, h = displaySize(displayFor("monitor"))
    if y >= h - 2 then
      if numericOnly or isPin then
        local half = math.max(1, math.floor((w - 5) / 2))
        if x < 3 + half then return nil end
        if isPin and #value ~= 6 then drawKeyboard("Enter exactly six digits before confirming.")
        else return value end
      else
        local half = math.max(1, math.floor((w - 6) / 5))
        local idx = math.floor((x - 2) / (half + 1)) + 1
        if idx == 4 then return nil
        elseif idx == 5 then return value
        elseif idx == 1 then value = value .. " "
        elseif idx == 2 then value = value:sub(1, -2)
        elseif idx == 3 then value = "" end
      end
    elseif numericOnly or isPin then
      local cw = math.max(2, math.floor((w - 5) / 3))
      if y >= 8 and y < 16 then
        local row = math.floor((y - 8) / 2)
        local col = math.floor((x - 2) / (cw + 1))
        local index = row * 3 + col + 1
        local keys = {"1","2","3","4","5","6","7","8","9","CLEAR","0","DEL"}
        local key = keys[index]
        if key then
          if key == "CLEAR" then value = ""
          elseif key == "DEL" then value = value:sub(1, -2)
          elseif #value < 64 and (not isPin or #value < 6) then value = value .. key end
        end
      end
    else
      local rows = {"1234567890", "QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM-_"}
      local cw = math.max(1, math.floor((w - 2) / 10))
      if y >= 9 and y <= 14 then
        local row = math.floor((y - 7) / 2)
        local col = math.floor((x - 2) / cw) + 1
        local rowText = rows[row]
        if rowText and col >= 1 and col <= #rowText and #value < 120 then value = value .. rowText:sub(col,col) end
      end
    end
    drawKeyboard()
  end
end

function M.secret(label)
  label = safeText(label or "Enter private value")
  if isTouchTarget() then
    local lower = label:lower()
    local pin = lower:find("pin", 1, true) or lower:find("passcode", 1, true)
    return touchInput(label, "", true, pin and true or false, pin and true or false)
  end
  return readTerminal(label, nil, true)
end

function M.prompt(label, default)
  label = safeText(label or "Enter a value")
  local defaultValue = tostring(default or "")
  if isTouchTarget() then
    local lower = label:lower()
    local numeric = lower:find("amount",1,true) or lower:find("number",1,true)
      or lower:find("emerald",1,true) or lower:find("price",1,true) or lower:find("quantity",1,true)
    local value = touchInput(label, defaultValue, false, numeric and true or false, false)
    if value == "" and default ~= nil then return defaultValue end
    return value
  end
  local value = readTerminal(label .. (default ~= nil and (" [" .. defaultValue .. "]") or ""), defaultValue, false)
  if value == "" and default ~= nil then return defaultValue end
  return value
end

function M.kioskIdle()
  local d = getMonitor()
  if not d then return false end
  withDisplay(function(display)
    local w, h = displaySize(display)
    drawHeader(display, "PAYMENT POINT", "MINEBANK SECURE CHECKOUT")
    drawButton(display, 2, 7, w - 3, math.max(2, math.min(4, h - 10)), "READY - WAITING FOR CASHIER", colors.green, colors.black)
    writeAt(display, 2, math.min(h - 3, 13), "Your cashier will send a purchase here.", colors.white, colors.black, w - 3)
    writeAt(display, 2, math.min(h - 2, 14), "No payment is requested while idle.", colors.lightGray, colors.black, w - 3)
  end, "monitor")
  return true
end

function M.line(label, value, good)
  return M.message(tostring(label or "") .. (value ~= nil and (": " .. tostring(value)) or ""), good)
end

function M.api(path, method, token, body, key, extraHeaders)
  if not http or type(http.request) ~= "function" then
    return nil, "Enable the CC:Tweaked HTTP API in server settings."
  end
  local headers = {Accept="application/json", ["Content-Type"]="application/json"}
  if token then headers.Authorization = "Bearer " .. token end
  if key then headers["Idempotency-Key"] = key end
  for name, value in pairs(extraHeaders or {}) do headers[name] = value end
  local url = M.API_BASE .. path
  local requestBody = body and textutils.serializeJSON(body) or nil
  local options = {url=url, method=method or (requestBody and "POST" or "GET"), headers=headers, body=requestBody}
  local ok, accepted, requestErr = pcall(http.request, options)
  if not ok then return nil, "Network request could not start: " .. tostring(accepted) end
  if not accepted then return nil, tostring(requestErr or "MineBank request was rejected before sending."), 0 end
  local timer = os.startTimer(10)
  while true do
    local event, a, b = os.pullEventRaw()
    if event == "http_success" and a == url then
      pcall(os.cancelTimer, timer)
      local response = b
      local status = response.getResponseCode and response.getResponseCode() or 200
      local raw = response.readAll()
      response.close()
      local parsed, data = pcall(textutils.unserializeJSON, raw)
      if not parsed or type(data) ~= "table" then return nil, "Invalid MineBank response (HTTP " .. status .. ").", status end
      if status < 200 or status >= 300 then return nil, tostring(data.error or ("HTTP " .. status)), status end
      return data, nil, status
    elseif event == "http_failure" and a == url then
      pcall(os.cancelTimer, timer)
      return nil, "Network request failed: " .. tostring(b or "unknown error")
    elseif event == "timer" and a == timer then
      return nil, "MineBank request timed out after 10 seconds. Its outcome is unknown; retry only with the saved idempotency key."
    elseif event == "terminate" then
      pcall(os.cancelTimer, timer)
      return nil, "Operation interrupted. Its outcome may be unknown; preserve the journal and reconcile before retrying."
    end
  end
end

function M.card()
  if not peripheral or type(peripheral.find) ~= "function" then return nil, "Drive peripheral API unavailable." end
  local okDrive, drive = pcall(peripheral.find, "drive")
  if not okDrive or not drive or not drive.isDiskPresent or not drive.isDiskPresent() then
    return nil, "Insert your MineBank floppy card."
  end
  local mount = drive.getMountPath and drive.getMountPath()
  if not mount or not fs.exists(mount) then return nil, "The floppy is not mounted." end
  local path = fs.combine(mount, M.CARD_FILE)
  if not fs.exists(path) then return nil, "No MineBank card file found on this floppy." end
  local h = fs.open(path, "r")
  if not h then return nil, "Cannot read the floppy card." end
  local raw = h.readAll()
  h.close()
  local ok, card = pcall(textutils.unserializeJSON, raw)
  if not ok or type(card) ~= "table" or type(card.credential) ~= "string" or not card.credential:match("^mbmc_") then
    return nil, "Invalid MineBank floppy card."
  end
  return card
end

function M.key(prefix)
  return tostring(prefix or "mb") .. "-" .. tostring(os.epoch("utc")) .. "-" .. tostring(os.getComputerID()) .. "-" .. tostring(math.random(100000,999999))
end

function M.inventory(name)
  if type(name) ~= "string" or name == "" then return nil, "empty peripheral name" end
  if not peripheral or type(peripheral.wrap) ~= "function" then return nil, "peripheral API unavailable" end
  local ok, inv = pcall(peripheral.wrap, name)
  if not ok then return nil, "wrap failed: " .. tostring(inv) end
  if type(inv) ~= "table" then return nil, "peripheral is not available" end
  local required = {"list", "size", "getItemDetail", "pushItems"}
  for _, method in ipairs(required) do
    if type(inv[method]) ~= "function" then return nil, "missing method " .. method end
  end
  local probeOk, probeErr = pcall(function()
    local size = inv.size()
    local contents = inv.list()
    if type(size) ~= "number" or size < 1 then error("invalid inventory size") end
    if type(contents) ~= "table" then error("list() did not return a table") end
  end)
  if not probeOk then return nil, "inventory probe failed: " .. tostring(probeErr) end
  return inv
end

local function peripheralTypeLabel(name)
  if peripheral and type(peripheral.getType) == "function" then
    local ok, value = pcall(peripheral.getType, name)
    if ok then
      if type(value) == "table" then table.sort(value); return table.concat(value, "/")
      elseif value ~= nil then return tostring(value) end
    end
  end
  return "unknown type"
end

function M.inventoryDiagnostics()
  local rows = {}
  if not peripheral or type(peripheral.getNames) ~= "function" then
    return {"Peripheral API unavailable; this must run on a CC:Tweaked computer."}
  end
  local ok, names = pcall(peripheral.getNames)
  if not ok or type(names) ~= "table" then return {"Could not enumerate peripherals: " .. tostring(names)} end
  table.sort(names)
  if #names == 0 then return {"No peripherals are visible to this computer."} end
  for _, name in ipairs(names) do
    local inv, reason = M.inventory(name)
    local label = tostring(name) .. " [" .. peripheralTypeLabel(name) .. "]"
    rows[#rows + 1] = inv and (label .. " - INVENTORY OK") or (label .. " - not usable as ATM storage: " .. tostring(reason or "unknown reason"))
  end
  return rows
end

function M.findInventory()
  local found = {}
  if not peripheral or type(peripheral.getNames) ~= "function" then return found end
  local ok, names = pcall(peripheral.getNames)
  if not ok or type(names) ~= "table" then return found end
  table.sort(names)
  for _, name in ipairs(names) do
    if M.inventory(name) then found[#found + 1] = name end
  end
  return found
end

function M.setup()
  M.clear()
  M.header("SETUP WIZARD", "MineBank terminal configuration")
  M.message("This wizard only saves local terminal configuration. It does not change your MineBank account or portal.", true)
  local base = M.prompt("HTTPS API base", M.API_BASE)
  if not base or not base:match("^https://") then return nil, "API base must use HTTPS." end
  M.API_BASE = base:gsub("/+$", "")
  return {api_base=M.API_BASE}
end

-- Ensure users running newer code on older installations get a usable renderer.
M.useTarget(target)
return M
