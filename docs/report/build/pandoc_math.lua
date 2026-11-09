-- Pandoc filter used only to build docs/TECHNICAL_REPORT.pdf.
-- 1. ```math fenced blocks (GitHub syntax) become display math.
-- 2. Table columns get widths in proportion to their content, so prose columns are not squeezed.

function CodeBlock(el)
  if el.classes:includes("math") then
    return pandoc.Para({pandoc.Math("DisplayMath", el.text)})
  end
end

local function cell_len(cell)
  local s = pandoc.utils.stringify(cell.contents or cell)
  return utf8.len(s) or #s
end

function Table(tbl)
  local n = #tbl.colspecs
  if n < 2 then return nil end
  local lens = {}
  for i = 1, n do lens[i] = 4 end
  local function scan(rows)
    for _, row in ipairs(rows) do
      for i, cell in ipairs(row.cells) do
        if i <= n then
          local l = cell_len(cell)
          lens[i] = math.max(lens[i], math.min(l, 60))
        end
      end
    end
  end
  scan(tbl.head.rows)
  for _, body in ipairs(tbl.bodies) do scan(body.body) end
  local total = 0
  for i = 1, n do lens[i] = math.max(lens[i], 9) ^ 0.7; total = total + lens[i] end
  for i = 1, n do
    tbl.colspecs[i] = {tbl.colspecs[i][1], 0.98 * lens[i] / total}
  end
  return tbl
end
