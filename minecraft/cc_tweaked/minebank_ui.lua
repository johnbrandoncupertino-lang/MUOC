-- MineBank shared CC:Tweaked utilities and Basalt2 touch UI.
-- The monitor is the interactive display; it is never just a keyboard prompt mirror.
local M = {}
M.API_BASE = "https://minebanksecurearea.vercel.app/api/v1/minecraft"
M.CARD_FILE = "minebank.card"

local monitor = peripheral and peripheral.find and peripheral.find("monitor")
if monitor and term and term.redirect then
  pcall(function() monitor.setTextScale(0.5) end)
  pcall(function() term.redirect(monitor) end)
end

local basalt
local basaltOk, basaltModule = pcall(require, "basalt")
if basaltOk then basalt = basaltModule end

function M.clear()
  term.setBackgroundColor(colors.black)
  term.setTextColor(colors.white)
  term.clear()
  term.setCursorPos(1,1)
end

function M.header(title, subtitle)
  local w = term.getSize()
  term.setBackgroundColor(colors.black); term.setTextColor(colors.lime)
  term.setCursorPos(1,1); term.write("MINEBANK")
  term.setTextColor(colors.lightGray); term.setCursorPos(1,2)
  term.write("THE NEW BANK  /  " .. tostring(title or "TERMINAL"):sub(1, math.max(1,w-18)))
  term.setTextColor(colors.gray); term.setCursorPos(1,3); term.write(string.rep("-", math.max(1,w)))
  if subtitle then
    term.setCursorPos(1,4); term.setTextColor(colors.white)
    term.write(tostring(subtitle):gsub("[\r\n]", " "):sub(1, math.max(1,w)))
  end
  term.setBackgroundColor(colors.black); term.setTextColor(colors.white)
end

local function addScreen(title, subtitle)
  if not basalt then return nil, "Basalt2 is not installed. Run install.lua." end
  local main = basalt.getMainFrame()
  local w,h = term.getSize()
  local screen = main:addFrame({x=1,y=1,width=w,height=h,background=colors.black})
  screen:addLabel({x=2,y=1,width=math.max(1,w-3),height=1,foreground=colors.lime,background=colors.black}):setText("MINEBANK")
  screen:addLabel({x=2,y=2,width=math.max(1,w-3),height=1,foreground=colors.lightGray,background=colors.black}):setText(tostring(title or "TERMINAL"):sub(1,math.max(1,w-3)))
  screen:addLabel({x=2,y=3,width=math.max(1,w-3),height=1,foreground=colors.gray,background=colors.black}):setText(string.rep("-",math.max(1,w-3)))
  if subtitle then screen:addLabel({x=2,y=4,width=math.max(1,w-3),height=2,foreground=colors.white,background=colors.black}):setText(tostring(subtitle):gsub("[\r\n]"," "):sub(1,math.max(1,w-3))) end
  return screen, nil, w, h
end

local function closeScreen(screen)
  if screen then pcall(function() screen:setVisible(false) end) end
end

function M.choose(title, options, subtitle)
  if not basalt then
    M.clear(); M.header(title, subtitle or "Type the option number.")
    for i, option in ipairs(options) do print(tostring(i)..") "..tostring(option)) end
    write("> "); local n=tonumber(read())
    if n and n%1==0 and options[n] then return n end
    return nil
  end
  local w,h=term.getSize()
  local pageSize=math.max(1,h-7)
  local page=1
  while true do
    local screen,err,sw,sh=addScreen(title,subtitle or "Touch an option to continue.")
    if not screen then M.message(err,false); return nil end
    local maxPage=math.max(1,math.ceil(#options/pageSize))
    local first=(page-1)*pageSize+1
    local last=math.min(#options,first+pageSize-1)
    local selected
    for i=first,last do
      local y=6+(i-first)
      local label=tostring(options[i])
      local maxText=math.max(1,sw-7)
      if #label>maxText then label=label:sub(1,math.max(1,maxText-1)).."…" end
      screen:addButton({x=2,y=y,width=math.max(1,sw-3),height=1,background=(i==#options and colors.gray or colors.green),foreground=colors.black})
        :setText(label):onClick(function() selected=i; basalt.stop() end)
    end
    if maxPage>1 then
      if page>1 then
        screen:addButton({x=2,y=sh-1,width=math.max(1,math.floor((sw-4)/2)),height=1,background=colors.gray,foreground=colors.white})
          :setText("Previous"):onClick(function() selected=-1; basalt.stop() end)
      end
      if page<maxPage then
        screen:addButton({x=3+math.floor((sw-4)/2),y=sh-1,width=math.max(1,sw-(3+math.floor((sw-4)/2))),height=1,background=colors.gray,foreground=colors.white})
          :setText("Next"):onClick(function() selected=-2; basalt.stop() end)
      end
    end
    basalt.run()
    closeScreen(screen)
    if selected==-1 then page=math.max(1,page-1)
    elseif selected==-2 then page=math.min(maxPage,page+1)
    else return selected end
  end
end
function M.message(text, good)
  local w = term.getSize()
  term.setTextColor(good and colors.lime or colors.orange)
  term.setCursorPos(1, math.min(6, select(2,term.getSize())))
  term.write(tostring(text):gsub("[\r\n]"," "):sub(1,math.max(1,w)))
  term.setTextColor(colors.white)
end

function M.pause()
  if basalt then
    M.choose("MINEBANK", {"Continue"}, "Touch Continue when you are ready.")
  else
    print(""); write("Press Enter to continue. "); read()
  end
end

function M.secret(label)
  local lower=tostring(label or ""):lower()
  if basalt and lower:find("pin") then
    local screen,err,w,h=addScreen("SECURE PIN",label or "Enter PIN")
    if screen and w>=12 and h>=14 then
      local value,result,done=""
      local masked=screen:addLabel({x=2,y=6,width=math.max(1,w-3),height=1,foreground=colors.lime,background=colors.black})
      local function redraw() masked:setText("PIN: "..string.rep("*",#value)) end
      local keys={"1","2","3","4","5","6","7","8","9","Clear","0","⌫"}
      local cellW=math.max(3,math.floor((w-4)/3))
      for i,key in ipairs(keys) do
        local row=math.floor((i-1)/3)
        local col=(i-1)%3
        screen:addButton({x=2+col*cellW,y=8+row*2,width=math.min(cellW-1,w-(2+col*cellW)),height=1,background=colors.gray,foreground=colors.white})
          :setText(key):onClick(function()
            if key=="Clear" then value=""
            elseif key=="⌫" then value=value:sub(1,-2)
            elseif #value<6 then value=value..key end
            redraw()
          end)
      end
      local y=math.min(h-1,15)
      screen:addButton({x=2,y=y,width=math.max(1,math.floor((w-4)/2)),height=1,background=colors.green,foreground=colors.black})
        :setText("Submit"):onClick(function() if #value==6 then result=value; done=true; basalt.stop() end end)
      screen:addButton({x=3+math.floor((w-4)/2),y=y,width=math.max(1,w-(3+math.floor((w-4)/2))),height=1,background=colors.red,foreground=colors.white})
        :setText("Cancel"):onClick(function() result=nil; done=true; basalt.stop() end)
      basalt.run()
      closeScreen(screen)
      if done then return result end
      return nil
    end
    closeScreen(screen)
  end
  write(tostring(label or ""))
  return read("*")
end

function M.prompt(label, default)
  if not basalt then
    if default then write(label.." ["..tostring(default).."]: ") else write(label..": ") end
    local v=read()
    if v=="" and default~=nil then return tostring(default) end
    return v
  end
  local screen,err,w,h=addScreen("INPUT",label)
  if not screen then return nil end
  local value=tostring(default or "")
  local input=screen:addInput({x=2,y=7,width=math.max(1,w-3),height=1,background=colors.white,foreground=colors.black})
  input:setText(value)
  local numeric= tostring(label):lower():find("amount") or tostring(label):lower():find("number") or tostring(label):lower():find("emerald")
  local result,done
  local function finish(v) result=v; done=true; basalt.stop() end
  if numeric and h>=15 then
    local digits={"1","2","3","4","5","6","7","8","9","0","⌫","Clear"}
    local cols=math.min(3,math.max(1,math.floor((w-4)/6)))
    local cellW=math.max(3,math.floor((w-4)/cols))
    for i,digit in ipairs(digits) do
      local row=math.floor((i-1)/cols)
      local col=(i-1)%cols
      local b=screen:addButton({x=2+col*cellW,y=9+row*2,width=math.min(cellW-1,w-(2+col*cellW)),height=1,background=colors.gray,foreground=colors.white})
      b:setText(digit)
      b:onClick(function()
        local current=input:getText() or ""
        if digit=="Clear" then current=""
        elseif digit=="⌫" then current=current:sub(1,-2)
        elseif #current<18 then current=current..digit end
        input:setText(current)
      end)
    end
    local buttonY=math.min(h-1,16)
    screen:addButton({x=2,y=buttonY,width=math.max(1,math.floor((w-4)/2)),height=1,background=colors.green,foreground=colors.black}):setText("Submit"):onClick(function() finish(input:getText()) end)
    screen:addButton({x=3+math.floor((w-4)/2),y=buttonY,width=math.max(1,w-(3+math.floor((w-4)/2))),height=1,background=colors.red,foreground=colors.white}):setText("Cancel"):onClick(function() finish(nil) end)
  else
    screen:addLabel({x=2,y=9,width=math.max(1,w-3),height=2,foreground=colors.gray,background=colors.black}):setText("Use the connected keyboard for this field.")
    screen:addButton({x=2,y=math.max(11,h-2),width=math.max(1,math.floor((w-4)/2)),height=1,background=colors.green,foreground=colors.black}):setText("Submit"):onClick(function() finish(input:getText()) end)
    screen:addButton({x=3+math.floor((w-4)/2),y=math.max(11,h-2),width=math.max(1,w-(3+math.floor((w-4)/2))),height=1,background=colors.red,foreground=colors.white}):setText("Cancel"):onClick(function() finish(nil) end)
  end
  basalt.run()
  closeScreen(screen)
  if result==nil and not done then return nil end
  return result
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
  if not accepted then return nil,tostring(requestErr or "MineBank request was rejected before sending.") end
  local timer=os.startTimer(10)
  while true do
    local event,a,b,c,d=os.pullEventRaw()
    if event=="http_success" and a==url then
      local response=b
      local status=response.getResponseCode and response.getResponseCode() or 200
      local raw=response.readAll(); response.close()
      local parsed,data=pcall(textutils.unserializeJSON,raw)
      if not parsed or type(data)~="table" then return nil,"Invalid MineBank response (HTTP "..status..").",status end
      if status<200 or status>=300 then return nil,tostring(data.error or ("HTTP "..status)),status end
      return data,nil,status
    elseif event=="http_failure" and a==url then
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

function M.inventory(name)
  if not name or name=="" then return nil end
  local ok,inv=pcall(peripheral.wrap,name)
  if not ok or not inv or not inv.list or not inv.pushItems then return nil end
  return inv
end

function M.findInventory()
  local names=peripheral.getNames(); local found={}
  for _,name in ipairs(names) do if M.inventory(name) then found[#found+1]=name end end
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

return M
