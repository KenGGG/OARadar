import type { ReactNode } from "react"

function cells(line: string): string[] {
  return line.trim().replace(/^\||\|$/g, "").split("|").map(cell => cell.trim())
}

function tableRule(line: string): boolean {
  const parts = cells(line)
  return parts.length > 1 && parts.every(part => /^:?-{3,}:?$/.test(part))
}

const listItem = /^\s*(?:[-*+]|\d+[.)])\s+(.+)$/
const heading = /^(#{1,6})\s+(.+)$/

export function MarkdownPreview({ text }: { text: string }) {
  const lines = text.replace(/\r\n/g, "\n").split("\n")
  const blocks: ReactNode[] = []
  let index = 0
  while (index < lines.length) {
    const line = lines[index]
    if (!line.trim()) { index++; continue }
    if (/^\s*```/.test(line)) {
      const content: string[] = []
      index++
      while (index < lines.length && !/^\s*```/.test(lines[index])) content.push(lines[index++])
      if (index < lines.length) index++
      blocks.push(<pre key={blocks.length}><code>{content.join("\n")}</code></pre>)
      continue
    }
    const title = line.match(heading)
    if (title) {
      const level = title[1].length
      const value = title[2]
      const key = blocks.length
      blocks.push(level === 1 ? <h1 key={key}>{value}</h1> : level === 2 ? <h2 key={key}>{value}</h2> : <h3 key={key}>{value}</h3>)
      index++
      continue
    }
    if (line.includes("|") && index + 1 < lines.length && tableRule(lines[index + 1])) {
      const headers = cells(line)
      index += 2
      const rows: string[][] = []
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) rows.push(cells(lines[index++]))
      blocks.push(<table key={blocks.length}><thead><tr>{headers.map((value, i) => <th key={i}>{value}</th>)}</tr></thead><tbody>{rows.map((row, i) => <tr key={i}>{row.map((value, j) => <td key={j}>{value}</td>)}</tr>)}</tbody></table>)
      continue
    }
    const first = line.match(listItem)
    if (first) {
      const ordered = /^\s*\d+[.)]/.test(line)
      const entries: string[] = []
      while (index < lines.length && lines[index].match(listItem) && /^\s*\d+[.)]/.test(lines[index]) === ordered) {
        entries.push(lines[index++].match(listItem)![1])
      }
      const children = entries.map((value, i) => <li key={i}>{value}</li>)
      blocks.push(ordered ? <ol key={blocks.length}>{children}</ol> : <ul key={blocks.length}>{children}</ul>)
      continue
    }
    const paragraph: string[] = [line.trim()]
    index++
    while (index < lines.length && lines[index].trim() && !heading.test(lines[index]) && !listItem.test(lines[index]) && !/^\s*```/.test(lines[index])) {
      if (lines[index].includes("|") && index + 1 < lines.length && tableRule(lines[index + 1])) break
      paragraph.push(lines[index++].trim())
    }
    blocks.push(<p key={blocks.length}>{paragraph.join(" ")}</p>)
  }
  return <article className="markdown-document">{blocks}</article>
}
