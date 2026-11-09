-- docx build only: replace ```mermaid blocks with PNGs rendered by mermaid-cli.
-- MMDC: the mmdc command (default: npx -y @mermaid-js/mermaid-cli@11.4.2).
-- PUPPETEER_CONFIG: optional puppeteer JSON (e.g. {"executablePath": "/path/to/chromium", "args": ["--no-sandbox"]}).
local MMDC = os.getenv("MMDC") or "npx -y @mermaid-js/mermaid-cli@11.4.2"
local PP = os.getenv("PUPPETEER_CONFIG")
local OUT = os.getenv("MERMAID_OUT") or "build_mermaid"
function CodeBlock(el)
  if not el.classes:includes("mermaid") then return nil end
  os.execute("mkdir -p " .. OUT)
  local name = pandoc.sha1(el.text)
  local src, png = OUT .. "/" .. name .. ".mmd", OUT .. "/" .. name .. ".png"
  local f = io.open(png, "r")
  if f then f:close() else
    local g = io.open(src, "w"); g:write(el.text); g:close()
    local pp = PP and (" -p " .. PP) or ""
    os.execute(MMDC .. " -q" .. pp .. " -s 2 -b white -w 1400 -i " .. src .. " -o " .. png)
  end
  local f2 = io.open(png, "r")
  if not f2 then return nil end   -- rendering failed: keep the code block
  f2:close()
  return pandoc.Para({pandoc.Image({}, png, "", {width = "6.5in"})})
end
