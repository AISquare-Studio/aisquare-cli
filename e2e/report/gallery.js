// gallery.html's only script, inlined by e2e/report/gallery.py. It adds the lightbox and
// nothing else: the page is complete without it, and every picture is a link to its PNG,
// so a click still enlarges it where scripts are off.
(function () {
  "use strict";
  var dialog = document.getElementById("lightbox");
  var links = Array.prototype.slice.call(document.querySelectorAll("a.shot"));
  if (!dialog || typeof dialog.showModal !== "function" || links.length === 0) return;

  var box = dialog.querySelector(".lb");
  var img = dialog.querySelector(".lb-img");
  var title = dialog.querySelector(".lb-title");
  var caption = dialog.querySelector(".lb-caption");
  var count = dialog.querySelector(".lb-count");
  var badge = dialog.querySelector(".lb-badge");
  var file = dialog.querySelector(".lb-file");
  var at = 0;
  var opener = null;

  function show(index) {
    at = (index + links.length) % links.length;
    var link = links[at];
    var thumb = link.querySelector("img");
    var tone = link.getAttribute("data-tone") || "s-passed";
    img.src = thumb.src; // the same data: URI, so nothing is fetched or duplicated
    img.alt = thumb.alt;
    title.textContent = link.getAttribute("data-title");
    caption.textContent = link.getAttribute("data-step");
    count.textContent = at + 1 + " / " + links.length;
    badge.className = "badge lb-badge " + tone;
    badge.textContent = link.getAttribute("data-label");
    file.href = link.getAttribute("href");
    box.classList.remove("actual");
  }

  links.forEach(function (link, index) {
    link.addEventListener("click", function (event) {
      // A modified or middle click keeps the browser's meaning: open the PNG itself.
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      opener = link;
      show(index);
      dialog.showModal();
    });
  });

  dialog.addEventListener("keydown", function (event) {
    if (event.key === "ArrowRight") {
      show(at + 1);
      event.preventDefault();
    } else if (event.key === "ArrowLeft") {
      show(at - 1);
      event.preventDefault();
    } else if (event.key === "Escape") {
      // <dialog> closes on Esc by itself, but not always on the first press after a click.
      dialog.close();
      event.preventDefault();
    }
  });
  dialog.querySelector(".lb-prev").addEventListener("click", function () { show(at - 1); });
  dialog.querySelector(".lb-next").addEventListener("click", function () { show(at + 1); });
  dialog.querySelector(".lb-close").addEventListener("click", function () { dialog.close(); });
  img.addEventListener("click", function () { box.classList.toggle("actual"); });
  dialog.addEventListener("click", function (event) {
    if (event.target === dialog || event.target.classList.contains("lb-stage")) dialog.close();
  });
  dialog.addEventListener("close", function () {
    img.removeAttribute("src");
    if (opener) opener.focus();
  });
})();
