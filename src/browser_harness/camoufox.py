"""Camoufox backend. The caller owns the context, page lease and input fences.

This module intentionally does not launch a browser or depend on CDP. Both the
Python agent and compiled JavaScript call these primitives through the host.
"""
import base64

VERSION = "camoufox-harness-v2"


async def execute(page, method, args):
    frame = page
    if args.get("frameUrl"):
        matches = [f for f in page.frames if f.url == args["frameUrl"]]
        if len(matches) != 1:
            raise ValueError("frameUrl must identify exactly one current frame")
        frame = matches[0]
    timeout = min(max(int(args.get("timeoutMs", 15000)), 1), 120000)
    if method == "page_info":
        return await page.evaluate("""() => ({url:location.href,title:document.title,
          w:innerWidth,h:innerHeight,sx:scrollX,sy:scrollY,
          pw:document.documentElement.scrollWidth,ph:document.documentElement.scrollHeight})""")
    if method == "accessibility_tree":
        target = frame.locator(args.get("selector", "body"))
        return {"tree": await target.aria_snapshot(timeout=timeout), "bounds": await target.bounding_box(timeout=timeout)}
    if method == "observe":
        selector = args.get("selector", "body")
        target = frame.locator(selector)
        return {"url": page.url, "title": await page.title(),
                "text": (await target.inner_text(timeout=timeout))[:20000],
                "aria": (await target.aria_snapshot(timeout=timeout))[:20000]}
    if method == "find":
        target = frame.locator(args["selector"])
        count = await target.count()
        result = []
        for i in range(min(count, int(args.get("limit", 50)), 100)):
            item = target.nth(i)
            result.append({"text": (await item.inner_text(timeout=timeout))[:4000],
                           "bounds": await item.bounding_box(timeout=timeout),
                           "visible": await item.is_visible()})
        return {"count": count, "elements": result}
    if method == "js":
        # Preserve statement completion values as well as expression/function calls.
        # Parse before execution so a failed expression cannot execute effects twice.
        return await frame.evaluate("""mw:async ({source, argument}) => {
          let expression;
          try { expression = new Function('argument', 'return (' + source + '\\n)'); }
          catch (error) { if (!(error instanceof SyntaxError)) throw error; }
          if (expression) {
            const value = expression(argument);
            return await (typeof value === 'function' ? value(argument) : value);
          }
          return await (0,eval)(source);
        }""", {"source": args["expression"], "argument": args.get("argument")})
    if method == "request":
        return await frame.evaluate("""mw:async ({url,method,body,headers,timeout}) => {
          const target = new URL(url, location.href);
          if (target.origin !== location.origin || !['http:','https:'].includes(target.protocol)) throw new Error('Browser requests must be same-origin');
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), timeout);
          try {
            const response = await fetch(target.href, {method, credentials:'same-origin',redirect:'error',
              headers: {'Content-Type':'application/json', ...headers},
              ...(body == null ? {} : {body: typeof body === 'string' ? body : JSON.stringify(body)}),signal:controller.signal});
            const text = await response.text();
            let data; try { data = JSON.parse(text); } catch { data = text; }
            return {status:response.status,ok:response.ok,url:response.url,data};
          } finally { clearTimeout(timer); }
        }""", {"url": args["url"], "method": args.get("method", "GET"), "body": args.get("body"), "headers": args.get("headers", {}), "timeout": timeout})
    if method == "click":
        await frame.locator(args["selector"]).click(timeout=timeout)
    elif method == "fill":
        await frame.locator(args["selector"]).fill(args["text"], timeout=timeout)
    elif method == "click_at_xy":
        await page.mouse.click(args["x"], args["y"], button=args.get("button", "left"), click_count=args.get("clicks", 1))
    elif method == "type_text":
        await page.keyboard.insert_text(args["text"])
    elif method == "press_key":
        names = [(1, "Alt"), (2, "Control"), (4, "Meta"), (8, "Shift")]
        prefix = [name for bit, name in names if int(args.get("modifiers", 0)) & bit]
        await page.keyboard.press("+".join(prefix + [args["key"]]))
    elif method == "clipboard_write":
        page._harness_clipboard = args["text"]
    elif method == "paste":
        text = args.get("text", getattr(page, "_harness_clipboard", ""))
        handled = await page.evaluate("""text => {
          const data = new DataTransfer(); data.setData('text/plain', text);
          return !document.activeElement.dispatchEvent(new ClipboardEvent('paste', {clipboardData:data,bubbles:true,cancelable:true}));
        }""", text)
        if not handled:
            await page.keyboard.insert_text(text)
    elif method == "scroll":
        await page.mouse.move(args.get("x", 0), args.get("y", 0))
        await page.mouse.wheel(args.get("dx", 0), args.get("dy", 300))
    elif method == "wait_for_element":
        await frame.locator(args["selector"]).wait_for(state=args.get("state", "visible"), timeout=timeout)
    elif method == "frames":
        return [{"url": f.url, "name": f.name} for f in page.frames]
    elif method == "screenshot":
        return {"base64": base64.b64encode(await page.screenshot(type="png")).decode("ascii")}
    elif method == "upload":
        await frame.locator(args["selector"]).set_input_files({"name": args["name"], "mimeType": args.get("mimeType", "application/octet-stream"),
                                                              "buffer": base64.b64decode(args["base64"], validate=True)}, timeout=timeout)
    elif method == "download":
        async with page.expect_download(timeout=timeout) as pending:
            await frame.locator(args["selector"]).click(timeout=timeout)
        download = await pending.value
        path = await download.path()
        with open(path, "rb") as source:
            data = source.read(8_000_001)
        if len(data) > 8_000_000:
            raise ValueError("download exceeds 8 MB")
        return {"name": download.suggested_filename, "base64": base64.b64encode(data).decode("ascii")}
    else:
        raise ValueError(f"Unknown Camoufox operation: {method}")
    return {"url": page.url}
