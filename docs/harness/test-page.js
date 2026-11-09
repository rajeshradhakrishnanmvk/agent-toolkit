import { runTests } from "./test.js";

const root = document.getElementById("results");
const summary = await runTests();
root.textContent = "";
const heading = document.createElement("p");
heading.className = summary.failed ? "fail" : "pass";
heading.textContent = summary.passed + " passed, " + summary.failed + " failed";
root.appendChild(heading);
summary.results.forEach(function (item) {
  const row = document.createElement("p");
  row.className = item.ok ? "pass" : "fail";
  row.textContent = (item.ok ? "ok " : "FAIL ") + item.name;
  if (!item.ok) {
    const detail = document.createElement("pre");
    detail.textContent = item.error;
    row.appendChild(detail);
  }
  root.appendChild(row);
});
