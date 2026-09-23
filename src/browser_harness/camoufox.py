"""Camoufox backend. The caller owns the context, page lease and input fences.

This module intentionally does not launch a browser or depend on CDP. Both the
Python agent and compiled JavaScript call these primitives through the host.
"""
import base64

VERSION = "camoufox-harness-v1"


async def execute(page, method, args):
    frame = page
    if args.get("frameUrl"):
        matches = [f for f in page.frames if f.url == args["frameUrl"]]
        if len(matches) != 1:
            raise ValueError("frameUrl must identify exactly one current frame")
        frame = matches[0]
    timeout = min(max(int(args.get("timeoutMs", 15000)), 1), 120000)
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
        # Explicit evaluation is an opaque effect, never silently treated as a read.
        return await frame.evaluate(args["expression"], args.get("argument"))
    if method == "click":
        await frame.locator(args["selector"]).click(timeout=timeout)
    elif method == "fill":
        await frame.locator(args["selector"]).fill(args["text"], timeout=timeout)
    elif method == "click_at_xy":
        await page.mouse.click(args["x"], args["y"], button=args.get("button", "left"), click_count=args.get("clicks", 1))
    elif method == "type_text":
        await page.keyboard.insert_text(args["text"])
    elif method == "press_key":
        await page.keyboard.press(args["key"])
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
