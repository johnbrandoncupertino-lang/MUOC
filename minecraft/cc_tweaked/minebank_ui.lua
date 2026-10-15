-- MineBank shared CC:Tweaked utilities and full Basalt2 touch interface.
-- Visual reference: a polished, information-dense CC:Tweaked app; banking-only behavior.
-- Computer and monitor are independent Basalt roots. The merchant can keep cashier
-- controls private on the computer while showing checkout steps on a customer monitor.
local M = {}
M.API_BASE = "https://minebanksecurearea.vercel.app/api/v1/minecraft"
M.CARD_FILE = "minebank.card"

local nativeTerm = (term.native and term.native()) or term.current()
local monitor = peripheral and peripheral.find and peripheral.find("monitor") or nil
if monitor then pcall(function() monitor.setTextScale(0.5) end) end

local basalt
local basaltLoadError = nil

-- This is a single-file Basalt2 release bundle. Load it directly from the
-- installation directory first; changing package.path is unnecessary and can
-- itself fail in restricted/custom CC:Tweaked environments.
local runningProgram = ""
pcall(function()
  if shell and shell.getRunningProgram then runningProgram = shell.getRunningProgram() or "" end
end)
local programDir = ""
pcall(function()
  if fs and fs.getDir then programDir = fs.getDir(runningProgram) or "" end
end)
local function isBasaltModule(value)
  return type(value) == "table" and type(value.getMainFrame) == "function"
end

-- Try the program's own folder and the standard installer directory. This
-- also supports launching a MineBank program from the computer root or a
-- startup launcher while the shared UI code lives under /minebank.
local basaltCandidates = {}
local function addBasaltCandidate(path)
  if type(path) ~= "string" or path == "" then return end
  for _, existing in ipairs(basaltCandidates) do
    if existing == path then return end
  end
  basaltCandidates[#basaltCandidates + 1] = path
end
if programDir ~= "" then
  pcall(function() addBasaltCandidate(fs.combine(programDir, "basalt.lua")) end)
end
addBasaltCandidate("basalt.lua")
addBasaltCandidate("/minebank/basalt.lua")
addBasaltCandidate("minebank/basalt.lua")

local attemptedBasaltPaths = {}
for _, path in ipairs(basaltCandidates) do
  local exists = false
  local existsOk, existsResult = pcall(function()
    return fs and fs.exists and fs.exists(path) or false
  end)
  if existsOk then exists = existsResult end
  attemptedBasaltPaths[#attemptedBasaltPaths + 1] = path .. (exists and " [exists]" or " [missing]")
  if exists then
    local loaded, module = pcall(dofile, path)
    if loaded and isBasaltModule(module) then
      basalt = module
      bundledBasalt = path
      basaltLoadError = nil
      break
    elseif not loaded then
      basaltLoadError = "dofile(" .. path .. ") failed: " .. tostring(module)
    else
      basaltLoadError = "The file " .. path .. " ran but returned no valid Basalt2 module."
    end
  end
end
if not basalt and not basaltLoadError then
  basaltLoadError = "Basalt bundle not found. Checked: " .. table.concat(attemptedBasaltPaths, ", ")
elseif not basalt and #attemptedBasaltPaths > 0 then
  basaltLoadError = tostring(basaltLoadError) .. " | Checked: " .. table.concat(attemptedBasaltPaths, ", ")
end

-- Optional fallback for environments that provide Basalt as an installed module.
if not basalt and type(require) == "function" then
  local loaded, module = pcall(require, "basalt")
  if loaded and isBasaltModule(module) then
    basalt = module
    basaltLoadError = nil
  elseif loaded then
    basaltLoadError = tostring(basaltLoadError or "") .. " | require('basalt') returned an invalid module."
  else
    basaltLoadError = tostring(basaltLoadError or "") .. " | require('basalt') failed: " .. tostring(module)
  end
end

local roots = {}
if basalt then
  -- Basalt2 keys its main frame by term.current(); create one frame under each
  -- redirected terminal rather than passing a peripheral as an ignored argument.
  local previous = term.current and term.current() or nativeTerm
  local terminalOk, terminalFrame = pcall(function()
    if term.redirect then term.redirect(nativeTerm) end
    return basalt.getMainFrame()
  end)
  if terminalOk and terminalFrame then
    roots.terminal = terminalFrame
  else
    basaltLoadError = "Basalt loaded, but terminal root creation failed: " .. tostring(terminalFrame)
  end
  if monitor then
    local monitorOk, monitorFrame = pcall(function()
      if term.redirect then term.redirect(monitor) end
      return basalt.getMainFrame()
    end)
    if monitorOk and monitorFrame then
      roots.monitor = monitorFrame
    else
      -- Monitor support is optional; terminal UI can still operate.
      roots.monitor = nil
    end
  end
  pcall(function()
    if term.redirect then term.redirect(previous or nativeTerm) end
  end)
  if not roots.terminal then basalt = nil end
end

local target = monitor and roots.monitor and "monitor" or "terminal"
local screens = {}

function M.hasBasalt()
  return basalt ~= nil
end

function M.showMissingBasalt()
  if basalt then return false end
  local d = nativeTerm
  local diagnostic = tostring(basaltLoadError or "Unknown Basalt2 loader error")
  diagnostic = diagnostic:gsub("[\r\n]+", " ")

  -- Save the untruncated message as a file so it can be inspected later.
  pcall(function()
    local logPath = fs.combine(programDir ~= "" and programDir or ".", "basalt_loader_error.txt")
    local h = fs.open(logPath, "w")
    if h then h.write(diagnostic); h.close() end
  end)

  pcall(function()
    local width, height = d.getSize()
    width, height = math.max(12, width or 51), math.max(8, height or 19)
    local textWidth = math.max(1, width - 4)
    local lines = {}
    for paragraph in diagnostic:gmatch("[^\r\n]+") do
      local rest = paragraph
      if rest == "" then lines[#lines + 1] = "" end
      while #rest > 0 do
        local chunk = rest:sub(1, textWidth)
        if #rest > textWidth then
          local split = chunk:match("^.*()%s+")
          if split and split > 1 then chunk = rest:sub(1, split - 1) end
        end
        lines[#lines + 1] = chunk
        rest = rest:sub(#chunk + 1):gsub("^%s+", "")
      end
    end
    if #lines == 0 then lines[1] = "No diagnostic text was returned." end

    local pageSize = math.max(1, height - 8)
    local pages = math.max(1, math.ceil(#lines / pageSize))
    for page = 1, pages do
      d.setBackgroundColor(colors.black)
      d.setTextColor(colors.lime)
      d.clear()
      d.setCursorPos(2, 2)
      d.write("MINEBANK / BASALT2 LOADER DIAGNOSTICS")
      d.setTextColor(colors.red)
      d.setCursorPos(2, 4)
      d.write("Touch UI unavailable. No banking operation started.")
      d.setTextColor(colors.white)
      local first = (page - 1) * pageSize + 1
      local last = math.min(#lines, first + pageSize - 1)
      for i = first, last do
        d.setCursorPos(2, 6 + i - first)
        d.write(lines[i]:sub(1, textWidth))
      end
      d.setTextColor(colors.lightGray)
      d.setCursorPos(2, height - 1)
      d.write(("Page %d/%d — press Enter for next page"):format(page, pages):sub(1, width - 3))
      read()
    end
    d.setTextColor(colors.white)
    d.setCursorPos(1, height)
    d.write("Full error saved to basalt_loader_error.txt")
  end)
  return true
end
local metaByFrame = {}
local terminalRedirect = nativeTerm

local function displayFor(name)
  if name == "monitor" and monitor then return monitor end
  return nativeTerm
end

local function rootFor(name)
  if not basalt then return nil end
  return roots[name] or roots.terminal
end

local function hideFrame(frame)
  if not frame then return end
  pcall(function() frame:setVisible(false) end)
  -- Basalt builds differ slightly in frame disposal support. Prefer disposal when
  -- available so a 24/7 terminal does not accumulate hundreds of hidden views.
  pcall(function() frame:remove() end)
end

local function closeScreen(name)
  local meta = screens[name]
  if meta then
    hideFrame(meta.frame)
    screens[name] = nil
  end
end

function M.useTarget(name)
  if name == "monitor" and monitor and roots.monitor then
    target = "monitor"
  else
    target = "terminal"
  end
  if term.redirect then pcall(term.redirect, displayFor(target)) end
  return target
end

function M.hasMonitor()
  return monitor ~= nil and roots.monitor ~= nil
end

local function currentDisplay()
  return displayFor(target)
end

local function displaySize()
  local display = currentDisplay()
  local ok, w, h = pcall(function() return display.getSize() end)
  if ok and type(w) == "number" and type(h) == "number" then return w, h end
  return 51, 19
end

local function safeText(value)
  return tostring(value or ""):gsub("[\r\n]+", " "):gsub("%s+", " ")
end

local function wrapText(value, width, maxLines)
  width = math.max(1, math.floor(width or 1))
  local lines = {}
  for paragraph in tostring(value or ""):gmatch("[^\r\n]+") do
    local line = ""
    for word in paragraph:gmatch("%S+") do
      if #line == 0 then
        line = word
      elseif #line + #word + 1 <= width then
        line = line .. " " .. word
      else
        lines[#lines + 1] = line
        line = word
      end
    end
    if #line > 0 then lines[#lines + 1] = line end
  end
  if #lines == 0 then lines[1] = "" end
  if maxLines and #lines > maxLines then
    while #lines > maxLines do table.remove(lines) end
    local last = lines[#lines] or ""
    if #last >= width then last = last:sub(1, math.max(1, width - 1)) end
    lines[#lines] = last .. (width > 1 and "…" or "")
  end
  return lines
end

local function makeScreen(title, subtitle)
  if not basalt then return nil, "Basalt2 is not installed. Install Basalt 2.5 beside the program." end
  closeScreen(target)
  local root = rootFor(target)
  local w, h = displaySize()
  w, h = math.max(12, w), math.max(8, h)
  local frame = root:addFrame({
    x = 1, y = 1, width = w, height = h,
    background = colors.black,
  })

  -- Header band, accent separator, side rail, content canvas and footer.
  local top = frame:addFrame({x=1, y=1, width=w, height=4, background=colors.green})
  top:addLabel({
    x=2, y=1, width=math.max(1, math.min(18, w-3)), height=1,
    foreground=colors.lime, background=colors.green,
  }):setText("MINEBANK")
  top:addLabel({
    x=2, y=2, width=math.max(1, w-4), height=1,
    foreground=colors.white, background=colors.green,
  }):setText(("THE NEW BANK  /  " .. safeText(title)):sub(1, math.max(1, w-3)))
  local sub = safeText(subtitle)
  if sub ~= "" then
    top:addLabel({
      x=2, y=3, width=math.max(1, w-3), height=1,
      foreground=colors.lightGray, background=colors.green,
    }):setText(sub:sub(1, math.max(1, w-3)))
  else
    top:addLabel({
      x=2, y=3, width=math.max(1, w-3), height=1,
      foreground=colors.lightGray, background=colors.green,
    }):setText("SECURE BANKING SERVICES")
  end
  if w >= 38 then
    top:addLabel({
      x=math.max(2,w-17), y=1, width=16, height=1,
      foreground=colors.white, background=colors.green,
    }):setText("SECURE SESSION")
  end
  frame:addFrame({x=1, y=4, width=w, height=1, background=colors.lime})

  local sideWidth = (w >= 54 and h >= 14) and 12 or 0
  local bodyX = sideWidth > 0 and 15 or 2
  local bodyW = math.max(1, w - bodyX - 1)
  local bodyH = math.max(1, h - 5)
  if sideWidth > 0 then
    local side = frame:addFrame({
      x=2, y=5, width=sideWidth, height=bodyH, background=colors.gray,
    })
    side:addLabel({x=2,y=2,width=sideWidth-2,height=1,foreground=colors.lime,background=colors.gray}):setText("WORKSPACE")
    side:addLabel({x=2,y=4,width=sideWidth-2,height=1,foreground=colors.white,background=colors.gray}):setText("OVERVIEW")
    side:addLabel({x=2,y=6,width=sideWidth-2,height=1,foreground=colors.white,background=colors.gray}):setText("SERVICES")
    side:addLabel({x=2,y=8,width=sideWidth-2,height=1,foreground=colors.white,background=colors.gray}):setText("SECURITY")
    side:addLabel({x=2,y=math.max(2,bodyH-2),width=sideWidth-2,height=2,foreground=colors.lightGray,background=colors.gray}):setText("MINEBANK\\nONLINE")
  end
  local body = frame:addFrame({
    x=bodyX, y=5, width=bodyW, height=bodyH, background=colors.black,
  })
  local footer = frame:addFrame({x=1,y=h,width=w,height=1,background=colors.green})
  footer:addLabel({
    x=2,y=1,width=math.max(1,w-3),height=1,
    foreground=colors.white,background=colors.green,
  }):setText("MINEBANK  •  THE NEW BANK  •  PROTECTED SESSION")

  local meta = {
    frame=frame, body=body, w=w, h=h,
    bodyW=bodyW, bodyH=bodyH, bodyX=bodyX, sideWidth=sideWidth,
    target=target, nextY=2,
  }
  screens[target] = meta
  metaByFrame[frame] = meta
  return meta, nil, w, h
end

function M.clear()
  closeScreen(target)
  if not basalt then
    local d = currentDisplay()
    pcall(d.setBackgroundColor, colors.black)
    pcall(d.setTextColor, colors.white)
    pcall(d.clear)
    pcall(d.setCursorPos, 1, 1)
  end
end

function M.header(title, subtitle)
  M.clear()
  if not basalt then
    local d = currentDisplay()
    local w = select(1, d.getSize())
    d.setBackgroundColor(colors.black); d.setTextColor(colors.lime)
    d.setCursorPos(1,1); d.write("MINEBANK")
    d.setTextColor(colors.lightGray); d.setCursorPos(1,2)
    d.write(("THE NEW BANK  /  " .. safeText(title)):sub(1, math.max(1,w)))
    d.setTextColor(colors.white); d.setCursorPos(1,3)
    if subtitle then d.write(safeText(subtitle):sub(1, math.max(1,w))) end
    return
  end
  local meta = makeScreen(title, subtitle)
  if meta and subtitle and safeText(subtitle) ~= "" then
    M.message(subtitle, true)
  end
end

function M.message(text, good)
  if not basalt then
    local d = currentDisplay()
    local w,h = d.getSize()
    local lines = wrapText(text, w, math.max(1,h-5))
    d.setTextColor(good and colors.lime or colors.orange)
    local y = 5
    for _, line in ipairs(lines) do
      if y > h-1 then break end
      d.setCursorPos(1,y); d.write(line:sub(1,w)); y=y+1
    end
    d.setTextColor(colors.white)
    return
  end
  local meta = screens[target]
  if not meta then
    local created = makeScreen("NOTICE", nil)
    meta = created
  end
  if not meta then return end
  local maxLines = math.max(1, math.min(5, meta.bodyH - meta.nextY - 2))
  local lines = wrapText(text, math.max(1,meta.bodyW-5), maxLines)
  local cardH = math.min(meta.bodyH-meta.nextY+1, #lines+2)
  if cardH < 1 then
    -- Roll the transcript forward on long-running status pages rather than
    -- drawing over controls or growing the tree without bound.
    M.header("STATUS", "Latest operation details")
    meta = screens[target]
    if not meta then return end
    lines = wrapText(text, math.max(1,meta.bodyW-5), math.max(1,math.min(5,meta.bodyH-4)))
    cardH = math.min(meta.bodyH-2, #lines+2)
  end
  local y = math.max(1, math.min(meta.bodyH-cardH, meta.nextY))
  local card = meta.body:addFrame({
    x=1,y=y,width=meta.bodyW,height=math.max(1,cardH),
    background=colors.gray,
  })
  local fg = good and colors.lime or colors.orange
  for i,line in ipairs(lines) do
    card:addLabel({
      x=2,y=i,width=math.max(1,meta.bodyW-4),height=1,
      foreground=fg,background=colors.gray,
    }):setText(line)
  end
  meta.nextY = y + cardH + 1
end

function M.pause()
  if not basalt then
    local d=currentDisplay()
    d.setCursorPos(1, math.max(1,select(2,d.getSize())-1))
    d.setTextColor(colors.lightGray); d.write("Press Enter to continue.")
    read()
    return
  end
  local meta = screens[target]
  if not meta then meta = makeScreen("NOTICE", "Review the message below.") end
  if not meta then return end
  local result
  local y=math.max(2,meta.bodyH-1)
  local x=math.max(2,math.floor((meta.bodyW-22)/2))
  meta.body:addButton({
    x=x,y=y,width=math.max(10,math.min(22,meta.bodyW-2)),height=1,
    background=colors.green,foreground=colors.white,
  }):setText("Continue"):onClick(function() result=true; basalt.stop() end)
  basalt.run()
  closeScreen(target)
  return result
end

function M.choose(title, options, subtitle)
  options = options or {}
  if not basalt then
    M.clear(); M.header(title, subtitle or "Choose an option by name.")
    local d=currentDisplay()
    for _, option in ipairs(options) do
      d.setTextColor(colors.lime); d.write("  > ")
      d.setTextColor(colors.white); d.write(safeText(option):sub(1,math.max(1,select(1,d.getSize())-4)))
      local _,cy=d.getCursorPos()
      if cy < select(2,d.getSize())-2 then d.setCursorPos(1,cy+1) end
    end
    d.setTextColor(colors.lightGray); d.write("Type the option exactly: ")
    local value=read()
    for i, option in ipairs(options) do if value==tostring(option) then return i end end
    return nil
  end
  local page=1
  while true do
    local meta,err=makeScreen(title, subtitle or "Select a service to continue.")
    if not meta then M.message(err,false); return nil end
    local cols = meta.bodyW >= 66 and 3 or (meta.bodyW >= 34 and 2 or 1)
    local tileH = meta.bodyH >= 12 and 3 or 2
    local rowsPerPage = math.max(1, math.floor((meta.bodyH-4)/(tileH+1)))
    local pageSize = math.max(1, rowsPerPage*cols)
    local pages = math.max(1, math.ceil(#options/pageSize))
    local first = (page-1)*pageSize+1
    local last = math.min(#options, first+pageSize-1)
    local selected
    local gap=1
    local cellW=math.max(1,math.floor((meta.bodyW-4-(cols-1)*gap)/cols))
    for i=first,last do
      local k=i-first
      local col=math.floor(k/rowsPerPage)
      local row=k%rowsPerPage
      local x=2+col*(cellW+gap)
      local y=2+row*(tileH+1)
      local label=safeText(options[i])
      local lines=wrapText(label,math.max(1,cellW-3),tileH)
      local cancel=label:lower():find("cancel",1,true) or label:lower():find("back",1,true) or label:lower():find("exit",1,true)
      local danger=label:lower():find("revoke",1,true) or label:lower():find("delete",1,true)
      local bg=danger and colors.red or (cancel and colors.gray or colors.green)
      local fg=cancel and colors.white or colors.black
      local button=meta.body:addButton({
        x=x,y=y,width=cellW,height=tileH,
        background=bg,foreground=fg,
      })
      button:setText(table.concat(lines,"\n"))
      button:onClick(function() selected=i; basalt.stop() end)
    end
    if pages>1 then
      local footerY=math.max(2,meta.bodyH-1)
      if page>1 then
        meta.body:addButton({x=2,y=footerY,width=math.max(8,math.floor((meta.bodyW-5)/2)),height=1,background=colors.gray,foreground=colors.white})
          :setText("Previous page"):onClick(function() selected=-1; basalt.stop() end)
      end
      if page<pages then
        meta.body:addButton({x=math.max(2,meta.bodyW-math.floor((meta.bodyW-5)/2)),y=footerY,width=math.max(8,math.floor((meta.bodyW-5)/2)),height=1,background=colors.gray,foreground=colors.white})
          :setText("Next page"):onClick(function() selected=-2; basalt.stop() end)
      end
    else
      meta.body:addLabel({x=2,y=math.max(2,meta.bodyH-1),width=math.max(1,meta.bodyW-3),height=1,foreground=colors.lightGray,background=colors.black})
        :setText("Touch a card to continue. No numeric menu codes.")
    end
    basalt.run()
    closeScreen(target)
    if selected==-1 then page=math.max(1,page-1)
    elseif selected==-2 then page=math.min(pages,page+1)
    else return selected end
  end
end

function M.secret(label)
  local lower=safeText(label):lower()
  if basalt and (lower:find("pin",1,true) or lower:find("passcode",1,true)) then
    local meta,err=makeScreen("SECURE INPUT",label or "Enter your six-digit Wallet PIN")
    if not meta then M.message(err,false); return nil end
    local value,result,done = "", nil, false
    local masked=meta.body:addLabel({
      x=2,y=2,width=math.max(1,meta.bodyW-4),height=1,
      foreground=colors.lime,background=colors.black,
    })
    local function redraw()
      masked:setText("ENTERED  " .. string.rep("*",#value) .. "  ("..#value.."/6)")
    end
    local keys={"1","2","3","4","5","6","7","8","9","Clear","0","Del"}
    local cols=3
    local cellW=math.max(3,math.floor((meta.bodyW-5)/cols))
    for i,key in ipairs(keys) do
      local row=math.floor((i-1)/cols)
      local col=(i-1)%cols
      meta.body:addButton({
        x=2+col*(cellW+1),y=4+row,width=math.max(2,cellW),height=1,
        background=colors.gray,foreground=colors.white,
      }):setText(key):onClick(function()
        if key=="Clear" then value=""
        elseif key=="Del" then value=value:sub(1,-2)
        elseif #value<6 then value=value..key end
        redraw()
      end)
    end
    local y=math.max(9,meta.bodyH-1)
    meta.body:addButton({x=2,y=y,width=math.max(1,math.floor((meta.bodyW-5)/2)),height=1,background=colors.green,foreground=colors.white})
      :setText("Confirm PIN"):onClick(function() if #value==6 then result=value; done=true; basalt.stop() end end)
    meta.body:addButton({x=math.max(3,meta.bodyW-math.floor((meta.bodyW-5)/2)),y=y,width=math.max(1,math.floor((meta.bodyW-5)/2)),height=1,background=colors.red,foreground=colors.white})
      :setText("Cancel"):onClick(function() result=nil; done=true; basalt.stop() end)
    redraw()
    basalt.run()
    closeScreen(target)
    if done then return result end
    return nil
  end
  if not basalt then
    local d=currentDisplay()
    d.setCursorPos(1, math.max(1,select(2,d.getSize())-2))
    d.write(safeText(label) .. ": ")
    return read("*")
  end
  local result
  local meta,err=makeScreen("PRIVATE INPUT",label or "Enter private value")
  if not meta then M.message(err,false); return nil end
  local input=meta.body:addInput({x=2,y=3,width=math.max(1,meta.bodyW-4),height=1,background=colors.white,foreground=colors.black})
  input:setText("")
  pcall(function() input:focus() end)
  local y=math.max(5,meta.bodyH-1)
  meta.body:addButton({x=2,y=y,width=math.max(1,math.floor((meta.bodyW-5)/2)),height=1,background=colors.green,foreground=colors.white})
    :setText("Continue"):onClick(function() result=input:getText(); basalt.stop() end)
  meta.body:addButton({x=math.max(3,meta.bodyW-math.floor((meta.bodyW-5)/2)),y=y,width=math.max(1,math.floor((meta.bodyW-5)/2)),height=1,background=colors.red,foreground=colors.white})
    :setText("Cancel"):onClick(function() result=nil; basalt.stop() end)
  basalt.run()
  closeScreen(target)
  return result
end

function M.prompt(label, default)
  local valueDefault=tostring(default or "")
  if not basalt then
    local d=currentDisplay()
    d.setCursorPos(1, math.max(1,select(2,d.getSize())-2))
    d.write(safeText(label) .. (default~=nil and (" ["..valueDefault.."]") or "") .. ": ")
    local value=read()
    if value=="" and default~=nil then return valueDefault end
    return value
  end
  local meta,err=makeScreen("WORKSPACE INPUT",label or "Enter a value")
  if not meta then M.message(err,false); return nil end
  local input=meta.body:addInput({
    x=2,y=2,width=math.max(1,meta.bodyW-4),height=1,
    background=colors.white,foreground=colors.black,
  })
  input:setText(valueDefault)
  pcall(function() input:focus() end)
  local lower=safeText(label):lower()
  local numeric=lower:find("amount",1,true) or lower:find("number",1,true) or lower:find("emerald",1,true) or lower:find("price",1,true) or lower:find("quantity",1,true)
  if numeric and meta.bodyW>=20 and meta.bodyH>=12 then
    local digits={"1","2","3","4","5","6","7","8","9","Clear","0","Del"}
    local cellW=math.max(3,math.floor((meta.bodyW-5)/3))
    for i,digit in ipairs(digits) do
      local row=math.floor((i-1)/3)
      local col=(i-1)%3
      meta.body:addButton({
        x=2+col*(cellW+1),y=4+row,width=cellW,height=1,
        background=colors.gray,foreground=colors.white,
      }):setText(digit):onClick(function()
        local current=input:getText() or ""
        if digit=="Clear" then current=""
        elseif digit=="Del" then current=current:sub(1,-2)
        elseif #current<24 then current=current..digit end
        input:setText(current)
      end)
    end
  else
    meta.body:addLabel({
      x=2,y=4,width=math.max(1,meta.bodyW-4),height=2,
      foreground=colors.lightGray,background=colors.black,
    }):setText("Use the connected keyboard for text. Only this field is captured.")
  end
  local result,done
  local buttonY=math.max(5,meta.bodyH-1)
  local half=math.max(1,math.floor((meta.bodyW-5)/2))
  meta.body:addButton({x=2,y=buttonY,width=half,height=1,background=colors.green,foreground=colors.white})
    :setText("Save / continue"):onClick(function() result=input:getText(); done=true; basalt.stop() end)
  meta.body:addButton({x=math.max(3,meta.bodyW-half),y=buttonY,width=half,height=1,background=colors.red,foreground=colors.white})
    :setText("Cancel"):onClick(function() result=nil; done=true; basalt.stop() end)
  basalt.run()
  closeScreen(target)
  if not done then return nil end
  if result=="" and default~=nil then return valueDefault end
  return result
end

function M.kioskIdle()
  if not monitor or not roots.monitor or not basalt then return false end
  local previous=target
  M.useTarget("monitor")
  local meta=makeScreen("PAYMENT POINT", "MINEBANK SECURE CHECKOUT")
  if meta then
    local heroH=math.max(3,math.min(6,meta.bodyH-5))
    local hero=meta.body:addFrame({x=2,y=2,width=math.max(1,meta.bodyW-4),height=heroH,background=colors.green})
    hero:addLabel({x=2,y=2,width=math.max(1,meta.bodyW-7),height=1,foreground=colors.lime,background=colors.green})
      :setText("MINEBANK  /  THE NEW BANK")
    hero:addLabel({x=2,y=3,width=math.max(1,meta.bodyW-7),height=math.max(1,heroH-3),foreground=colors.white,background=colors.green})
      :setText("Your cashier will send a purchase here.\nReview the amount, choose a payment source, and authorise securely.")
    local statusY=math.max(2,meta.bodyH-3)
    meta.body:addLabel({x=2,y=statusY,width=math.max(1,meta.bodyW-4),height=1,foreground=colors.lime,background=colors.black})
      :setText("READY  •  WAITING FOR CASHIER")
    meta.body:addLabel({x=2,y=statusY+1,width=math.max(1,meta.bodyW-4),height=1,foreground=colors.lightGray,background=colors.black})
      :setText("No payment is requested. Your card is not needed yet.")
  end
  M.useTarget(previous)
  return meta ~= nil
end

function M.line(label, value, good)
  return M.message(tostring(label or "") .. (value~=nil and (": " .. tostring(value)) or ""), good)
end

function M.api(path, method, token, body, key, extraHeaders)
  if not http then return nil, "Enable the CC:Tweaked HTTP API in server settings." end
  local headers={Accept="application/json",["Content-Type"]="application/json"}
  if token then headers.Authorization="Bearer "..token end
  if key then headers["Idempotency-Key"]=key end
  for name,value in pairs(extraHeaders or {}) do headers[name]=value end
  local url=M.API_BASE..path
  local requestBody=body and textutils.serializeJSON(body) or nil
  local ok,accepted,requestErr=pcall(http.request,url,requestBody,headers)
  if not ok then return nil,"Network request could not start: "..tostring(accepted) end
  if not accepted then return nil,tostring(requestErr or "MineBank request was rejected before sending."),0 end
  local timer=os.startTimer(10)
  while true do
    local event,a,b,c,d=os.pullEventRaw()
    if event=="http_success" and a==url then
      pcall(os.cancelTimer,timer)
      local response=b
      local status=response.getResponseCode and response.getResponseCode() or 200
      local raw=response.readAll(); response.close()
      local parsed,data=pcall(textutils.unserializeJSON,raw)
      if not parsed or type(data)~="table" then return nil,"Invalid MineBank response (HTTP "..status..").",status end
      if status<200 or status>=300 then return nil,tostring(data.error or ("HTTP "..status)),status end
      return data,nil,status
    elseif event=="http_failure" and a==url then
      pcall(os.cancelTimer,timer)
      return nil,"Network request failed: "..tostring(b or "unknown error")
    elseif event=="timer" and a==timer then
      return nil,"MineBank request timed out after 10 seconds. Its outcome is unknown; retry only with the saved idempotency key."
    elseif event=="terminate" then
      return nil,"Operation interrupted. Its outcome may be unknown; preserve the journal and reconcile before retrying."
    end
  end
end
function M.card()
  local drive=peripheral.find("drive")
  if not drive or not drive.isDiskPresent or not drive.isDiskPresent() then return nil,"Insert your MineBank floppy card." end
  local mount=drive.getMountPath and drive.getMountPath()
  if not mount or not fs.exists(mount) then return nil,"The floppy is not mounted." end
  local path=fs.combine(mount,M.CARD_FILE)
  if not fs.exists(path) then return nil,"No MineBank card file found on this floppy." end
  local h=fs.open(path,"r"); if not h then return nil,"Cannot read the floppy card." end
  local raw=h.readAll(); h.close()
  local ok,card=pcall(textutils.unserializeJSON,raw)
  if not ok or type(card)~="table" or type(card.credential)~="string" or not card.credential:match("^mbmc_") then return nil,"Invalid MineBank floppy card." end
  return card
end

function M.key(prefix)
  return tostring(prefix or "mb").."-"..tostring(os.epoch("utc")).."-"..tostring(os.getComputerID()).."-"..tostring(math.random(100000,999999))
end

-- Return a usable inventory wrapper, with a diagnostic reason when it is not one.
-- Probe the methods the ATM actually uses instead of silently treating every
-- peripheral-wrap failure as "no chest connected".
function M.inventory(name)
  if type(name)~="string" or name=="" then return nil,"empty peripheral name" end
  if not peripheral or type(peripheral.wrap)~="function" then return nil,"peripheral API unavailable" end
  local ok,inv=pcall(peripheral.wrap,name)
  if not ok then return nil,"wrap failed: "..tostring(inv) end
  if type(inv)~="table" then return nil,"peripheral is not available" end
  local required={"list","size","getItemDetail","pushItems"}
  for _,method in ipairs(required) do
    if type(inv[method])~="function" then return nil,"missing method "..method end
  end
  local probeOk,probeErr=pcall(function()
    local size=inv.size()
    local contents=inv.list()
    if type(size)~="number" or size<1 then error("invalid inventory size") end
    if type(contents)~="table" then error("list() did not return a table") end
  end)
  if not probeOk then return nil,"inventory probe failed: "..tostring(probeErr) end
  return inv
end

local function peripheralTypeLabel(name)
  if peripheral and type(peripheral.getType)=="function" then
    local ok,value=pcall(peripheral.getType,name)
    if ok then
      if type(value)=="table" then
        table.sort(value)
        return table.concat(value,"/")
      elseif value~=nil then
        return tostring(value)
      end
    end
  end
  return "unknown type"
end

function M.inventoryDiagnostics()
  local rows={}
  if not peripheral or type(peripheral.getNames)~="function" then
    return {"Peripheral API unavailable; this must run on a CC:Tweaked computer."}
  end
  local ok,names=pcall(peripheral.getNames)
  if not ok or type(names)~="table" then
    return {"Could not enumerate peripherals: "..tostring(names)}
  end
  table.sort(names)
  if #names==0 then return {"No peripherals are visible to this computer."} end
  for _,name in ipairs(names) do
    local inv,reason=M.inventory(name)
    local label=tostring(name).." ["..peripheralTypeLabel(name).."]"
    if inv then
      rows[#rows+1]=label.." — INVENTORY OK"
    else
      rows[#rows+1]=label.." — not usable as ATM storage: "..tostring(reason or "unknown reason")
    end
  end
  return rows
end

function M.findInventory()
  local found={}
  if not peripheral or type(peripheral.getNames)~="function" then return found end
  local ok,names=pcall(peripheral.getNames)
  if not ok or type(names)~="table" then return found end
  table.sort(names)
  for _,name in ipairs(names) do
    if M.inventory(name) then found[#found+1]=name end
  end
  return found
end

function M.setup()
  M.clear(); M.header("SETUP WIZARD","MineBank terminal configuration")
  print("This wizard only saves local terminal configuration.")
  print("It does not change your MineBank account or portal.")
  local base=M.prompt("HTTPS API base",M.API_BASE)
  if not base or not base:match("^https://") then return nil,"API base must use HTTPS." end
  M.API_BASE=base:gsub("/+$","")
  return {api_base=M.API_BASE}
end

M.useTarget(target)
return M
