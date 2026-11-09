-- docx build only: drop "Back to the index" lines, turn links to the .md chapter files into plain text,
-- and drop the index's H1 (the document title comes from metadata).
local first_h1 = true
function Header(h)
  if h.level == 1 and first_h1 then first_h1 = false; return {} end
end
function Para(p)
  local txt = pandoc.utils.stringify(p)
  if txt == "Back to the index" then return {} end
end
function Link(l)
  if l.target:match("%.md$") or l.target:match("%.md#") then return l.content end
end
