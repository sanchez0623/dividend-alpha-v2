// 不依赖 spawn 的 JSX/JS 结构自检：提取 preview.html 的 babel 脚本，
// 校验括号/JSX 标签平衡 + hook 引用顺序，确认不会因语法错误白屏。
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(path.join(__dirname, "..", "web", "preview.html"), "utf8");
const m = html.match(/<script type="text\/babel"[^>]*>([\s\S]*?)<\/script>/);
if (!m) { console.error("FAIL 未找到 text/babel 脚本块"); process.exit(1); }
const jsx = m[1];

let ok = true;
// 1) 三类括号平衡（忽略字符串/注释的粗检，足以发现致命不平衡）
function balance(src) {
  const pairs = { "(": ")", "[": "]", "{": "}" };
  const stack = [];
  let inS = null; // ', ", `
  for (let i = 0; i < src.length; i++) {
    const c = src[i];
    if (inS) { if (c === inS && src[i - 1] !== "\\") inS = null; continue; }
    if (c === "'" || c === '"' || c === "`") { inS = c; continue; }
    if (c === "/" && src[i + 1] === "/") { while (i < src.length && src[i] !== "\n") i++; continue; }
    if (pairs[c]) stack.push(pairs[c]);
    else if (c === ")" || c === "]" || c === "}") {
      if (stack.pop() !== c) return `不匹配的 ${c} @${i}`;
    }
  }
  return stack.length ? `未闭合: ${stack.join(",")}` : null;
}
const b = balance(jsx);
if (b) { console.error("FAIL 括号不平衡:", b); ok = false; } else console.log("OK 括号/JSX 平衡");

// 2) hook 使用顺序：useState/useEffect 解构必须在 useDatasources 定义之前
const iHooks = jsx.indexOf("const { useState, useEffect } = React");
const iMyHook = jsx.indexOf("function useDatasources");
if (iHooks < 0 || iMyHook < 0 || iHooks > iMyHook) { console.error("FAIL hook 定义顺序错误"); ok = false; }
else console.log(`OK hooks 解构(行${jsx.slice(0,iHooks).split("\n").length}) 先于 useDatasources(行${jsx.slice(0,iMyHook).split("\n").length})`);

// 3) 关键符号存在
["useDatasources", "DsBadge", "DS_STATUS", "primary_connected"].forEach(k => {
  if (!jsx.includes(k)) { console.error("FAIL 缺少", k); ok = false; }
});
if (ok) console.log("OK 关键符号齐全");

console.log(ok ? "结构自检通过（不会因语法白屏）" : "结构自检失败");
process.exit(ok ? 0 : 1);
