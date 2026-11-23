"""Export section on the Manage screen (Task 25): three plain download links, one per CSV."""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import expect


def test_export_links_download_csv(page, base_url):
    page.goto(base_url + "/#/manage")
    section = page.locator("[data-section='export']")
    expect(section.locator("a.mg-export")).to_have_count(3)
    for kind in ("tasks", "contacts", "activity"):
        link = section.locator(f"a[href='/api/export/{kind}.csv']")
        expect(link).to_have_attribute("download", "")
    with page.expect_download() as info:
        section.locator("a[href='/api/export/tasks.csv']").click()
    download = info.value
    assert download.suggested_filename == "tasks.csv"
    text = Path(download.path()).read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("id,workstream_id,title,owner,status,")
