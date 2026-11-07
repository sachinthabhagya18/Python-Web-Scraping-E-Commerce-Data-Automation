#!/usr/bin/env python3
"""
Helly Wild Women's Jeans Scraper (Top 10 Products + Full Details + High-Res Photos)
Target Category: Women's Jeans Sri Lanka | High Rise & Wide Leg Styles | Helly Wild – My Store
Target URL: https://hellywild.lk/collections/jeans

Features:
1. Scrapes the first 10 products (or any custom --limit) from https://hellywild.lk/collections/jeans
2. Dual-Engine Extraction:
   - Engine A: Shopify Collection & Product JSON API (/collections/jeans/products.json & /products/<handle>.json)
   - Engine B: Full HTML Page Scraping via BeautifulSoup4 (extracts HTML descriptions, fabric/length specs,
     variant dropdowns, and high-res image tags if JSON endpoints are restricted)
3. High-Resolution Image Downloader:
   - Strips Shopify thumbnail resize parameters (?width=533) to download original full-resolution photos
   - Organizes photos into per-product folders: helly_wild_scraped_data/images/<01_product-slug>/
4. Multi-Format Data Export:
   - products_full_details.json       (Complete structured data including all variants, sizes, descriptions, images)
   - products_summary.csv             (Clean overview table of all 10 products)
   - product_variants_inventory.csv   (Every size variant with price, SKU, and In-Stock / Sold-Out status)
   - woocommerce_import_ready.csv     (Ready to import directly into WordPress WooCommerce -> Products -> Import)

Usage:
    pip install requests beautifulsoup4
    python helly_wild_jeans_scraper.py --limit 10 --output ./helly_wild_scraped_data
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://hellywild.lk"
COLLECTION_URL = "https://hellywild.lk/collections/jeans"
COLLECTION_JSON_URL = "https://hellywild.lk/collections/jeans/products.json"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def clean_html_to_text(html_content: str) -> str:
    """Convert raw HTML description into clean, readable plain text."""
    if not html_content:
        return ""
    soup = BeautifulSoup(html_content, "html.parser")
    for br in soup.find_all(["br", "p", "li", "div", "h1", "h2", "h3", "h4"]):
        br.append("\n")
    text = soup.get_text(separator=" ")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join([line for line in lines if line])


def get_high_res_image_url(img_url: str) -> str:
    """
    Convert Shopify CDN thumbnail URLs (e.g. ?v=123&width=533 or _600x.jpg)
    into full-resolution original image URLs.
    """
    if not img_url:
        return ""
    if img_url.startswith("//"):
        img_url = "https:" + img_url
    elif img_url.startswith("/"):
        img_url = urljoin(BASE_URL, img_url)

    # Remove Shopify _WIDTHxHEIGHT size suffixes before extension
    img_url = re.sub(r"_(\d+x\d*|\d*x\d+|small|medium|large|grande|compact|master)(?=\.(jpg|jpeg|png|webp))", "", img_url, flags=re.IGNORECASE)

    # Strip width/height query params while keeping version (?v=...)
    parsed = urlparse(img_url)
    query_parts = [
        part for part in parsed.query.split("&")
        if part and not part.lower().startswith(("width=", "height=", "crop="))
    ]
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "&".join(query_parts), ""))


def extract_specs_from_description(description_text: str) -> Dict[str, str]:
    """Extract key denim details (Material/Fabric, Length, Rise/Fit, Sizing Note) from description text."""
    specs = {
        "material_fabric": "",
        "length_inches": "",
        "fit_and_rise": "",
        "sizing_note": "",
    }
    if not description_text:
        return specs

    # Material / Fabric
    mat_match = re.search(r"(?:Material|Fabric)\s*[:\-–]\s*([^\n\.]+)", description_text, re.IGNORECASE)
    if mat_match:
        specs["material_fabric"] = mat_match.group(1).strip()
    elif "non-stretch" in description_text.lower():
        specs["material_fabric"] = "100% Premium Non-Stretch Cotton Denim"
    elif "stretchable" in description_text.lower() or "stretch" in description_text.lower():
        specs["material_fabric"] = "Premium Stretchable Denim"

    # Length
    len_match = re.search(r"(?:Length|Lengths Available|Inseam/Outseam lengths?)\s*[:\-–]?\s*([0-9\.\"\s\-–to]+(?:inches|\")?)", description_text, re.IGNORECASE)
    if len_match:
        specs["length_inches"] = len_match.group(1).strip()

    # Fit & Rise
    fit_match = re.search(r"(?:Fit\s*(?:&\s*Rise)?|Style)\s*[:\-–]\s*([^\n\.]+)", description_text, re.IGNORECASE)
    if fit_match:
        specs["fit_and_rise"] = fit_match.group(1).strip()

    # Sizing Note
    size_match = re.search(r"(?:Sizing|Size\s*&\s*Fit\s*Note)\s*[:\-–]\s*([^\n]+)", description_text, re.IGNORECASE)
    if size_match:
        specs["sizing_note"] = size_match.group(1).strip()

    return specs


def calculate_installments(price_lkr: float) -> Dict[str, str]:
    """Calculate Mintpay (3x), Koko (3x), and Payzy (4x) installment amounts shown on Helly Wild."""
    if price_lkr <= 0:
        return {"mintpay_3x": "0.00", "koko_3x": "0.00", "payzy_4x": "0.00"}
    return {
        "mintpay_3x": f"Rs {price_lkr / 3:,.2f}",
        "koko_3x": f"Rs {price_lkr / 3:,.2f}",
        "payzy_4x": f"Rs {price_lkr / 4:,.2f}",
    }


def download_product_images(
    session: requests.Session,
    image_urls: List[str],
    product_index: int,
    product_handle: str,
    images_base_dir: str,
) -> List[str]:
    """Download all product images into a dedicated folder and return local file paths."""
    folder_name = f"{product_index:02d}_{re.sub(r'[^a-zA-Z0-9_-]', '-', product_handle)}"
    product_img_dir = os.path.join(images_base_dir, folder_name)
    os.makedirs(product_img_dir, exist_ok=True)

    downloaded_paths: List[str] = []
    for idx, raw_url in enumerate(image_urls, start=1):
        clean_url = get_high_res_image_url(raw_url)
        if not clean_url:
            continue

        # Determine file extension
        path_part = urlparse(clean_url).path
        ext = os.path.splitext(path_part)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            ext = ".jpg"

        filename = f"{product_handle}_photo_{idx:02d}{ext}"
        local_path = os.path.join(product_img_dir, filename)

        try:
            resp = session.get(clean_url, timeout=25, stream=True)
            resp.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            downloaded_paths.append(local_path)
            print(f"      [Photo {idx}/{len(image_urls)}] Saved -> {local_path}")
        except Exception as exc:
            print(f"      [!] Failed to download image {clean_url}: {exc}")

    return downloaded_paths


def parse_shopify_product_json(
    product_data: Dict[str, Any],
    rank: int,
    session: requests.Session,
    images_dir: str,
    download_photos: bool = True,
) -> Dict[str, Any]:
    """Normalize a Shopify product JSON object into a rich product dictionary."""
    handle = product_data.get("handle", f"product-{rank}")
    title = product_data.get("title", "").strip()
    product_url = f"{BASE_URL}/products/{handle}"
    body_html = product_data.get("body_html") or ""
    description_text = clean_html_to_text(body_html)
    specs = extract_specs_from_description(description_text)

    # Extract variants (Sizes, Prices, Stock, SKUs)
    raw_variants = product_data.get("variants", [])
    variants_list: List[Dict[str, Any]] = []
    available_sizes: List[str] = []
    sold_out_sizes: List[str] = []
    all_sizes: List[str] = []

    prices: List[float] = []
    compare_prices: List[float] = []

    for v in raw_variants:
        size_label = str(v.get("option1") or v.get("title") or "").strip()
        v_price = float(v.get("price") or 0.0)
        v_compare = float(v.get("compare_at_price") or 0.0) if v.get("compare_at_price") else v_price
        is_available = bool(v.get("available", False))
        sku = v.get("sku") or f"HW-{product_data.get('id', rank)}-{size_label}"

        if v_price > 0:
            prices.append(v_price)
        if v_compare > 0:
            compare_prices.append(v_compare)

        all_sizes.append(size_label)
        if is_available:
            available_sizes.append(size_label)
        else:
            sold_out_sizes.append(size_label)

        variants_list.append({
            "variant_id": v.get("id"),
            "size": size_label,
            "price_lkr": v_price,
            "compare_at_price_lkr": v_compare,
            "sku": sku,
            "in_stock": is_available,
            "stock_status": "In Stock" if is_available else "Sold Out",
            "grams": v.get("grams", 0),
        })

    min_price = min(prices) if prices else 0.0
    max_compare = max(compare_prices) if compare_prices else min_price
    overall_in_stock = len(available_sizes) > 0

    # Extract all high-res image URLs
    raw_images = product_data.get("images", [])
    image_urls = [get_high_res_image_url(img.get("src", "")) for img in raw_images if img.get("src")]
    image_alt_texts = [img.get("alt") or title for img in raw_images if img.get("src")]

    local_photo_paths: List[str] = []
    if download_photos and image_urls:
        local_photo_paths = download_product_images(
            session=session,
            image_urls=image_urls,
            product_index=rank,
            product_handle=handle,
            images_base_dir=images_dir,
        )

    tags = product_data.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    return {
        "rank": rank,
        "product_id": product_data.get("id"),
        "title": title,
        "handle": handle,
        "product_url": product_url,
        "vendor": product_data.get("vendor", "Helly Wild"),
        "product_type": product_data.get("product_type", "Women's Jeans"),
        "category": "Women's Jeans",
        "regular_price_lkr": max_compare,
        "sale_price_lkr": min_price,
        "price_formatted": f"Rs {min_price:,.2f}",
        "installments": calculate_installments(min_price),
        "overall_in_stock": overall_in_stock,
        "overall_stock_status": "In Stock" if overall_in_stock else "Sold Out",
        "all_sizes": all_sizes,
        "in_stock_sizes": available_sizes,
        "sold_out_sizes": sold_out_sizes,
        "variants_count": len(variants_list),
        "variants": variants_list,
        "specifications": specs,
        "description_text": description_text,
        "description_html": body_html,
        "tags": tags,
        "published_at": product_data.get("published_at", ""),
        "updated_at": product_data.get("updated_at", ""),
        "image_count": len(image_urls),
        "featured_image_url": image_urls[0] if image_urls else "",
        "image_urls": image_urls,
        "image_alt_texts": image_alt_texts,
        "downloaded_photo_paths": local_photo_paths,
    }


def scrape_product_html_fallback(
    session: requests.Session,
    product_url: str,
    rank: int,
    images_dir: str,
    download_photos: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    Fallback scraper that parses a single product page's HTML & embedded JSON
    if the collection products.json endpoint is disabled.
    """
    parsed_path = urlparse(product_url).path.rstrip("/")
    handle = parsed_path.split("/")[-1]

    # First try the single product .json endpoint
    try:
        json_resp = session.get(f"{BASE_URL}/products/{handle}.json", timeout=20)
        if json_resp.status_code == 200:
            pdata = json_resp.json().get("product")
            if pdata:
                return parse_shopify_product_json(pdata, rank, session, images_dir, download_photos)
    except Exception:
        pass

    # Pure HTML fallback parsing
    resp = session.get(product_url, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    title_el = soup.find("h1")
    title = title_el.get_text(strip=True) if title_el else handle.replace("-", " ").title()

    # Price extraction
    price_text = ""
    price_el = soup.select_one(".price__regular .price-item--regular, .price-item--sale, .product__price")
    if price_el:
        price_text = price_el.get_text(strip=True)
    price_match = re.search(r"([0-9,]+(?:\.\d{2})?)", price_text.replace("Rs", ""))
    price_val = float(price_match.group(1).replace(",", "")) if price_match else 0.0

    # Description extraction
    desc_el = soup.select_one(".product__description, .rte, #tab-description")
    desc_html = str(desc_el) if desc_el else ""
    desc_text = clean_html_to_text(desc_html)
    specs = extract_specs_from_description(desc_text)

    # Variant options parsing (e.g., "26 - Sold out - Rs 5,650.00")
    variants_list: List[Dict[str, Any]] = []
    all_sizes: List[str] = []
    in_stock_sizes: List[str] = []
    sold_out_sizes: List[str] = []

    for opt in soup.select("select[name='id'] option, .product-form__input input[type='radio']"):
        opt_text = opt.get_text(" ", strip=True) if opt.name == "option" else (opt.get("value") or "")
        if not opt_text:
            continue
        size_m = re.match(r"^(\d+|[A-Z0-9]+)", opt_text)
        size_label = size_m.group(1) if size_m else opt_text
        is_sold_out = "sold out" in opt_text.lower() or opt.has_attr("disabled")
        all_sizes.append(size_label)
        if is_sold_out:
            sold_out_sizes.append(size_label)
        else:
            in_stock_sizes.append(size_label)
        variants_list.append({
            "variant_id": opt.get("value", ""),
            "size": size_label,
            "price_lkr": price_val,
            "compare_at_price_lkr": price_val,
            "sku": f"HW-{handle}-{size_label}",
            "in_stock": not is_sold_out,
            "stock_status": "Sold Out" if is_sold_out else "In Stock",
            "grams": 0,
        })

    # Image URLs
    image_urls: List[str] = []
    for img in soup.select(".product__media img, .product-gallery img, meta[property='og:image']"):
        raw_src = img.get("content") if img.name == "meta" else (img.get("data-src") or img.get("src") or "")
        if raw_src and ("/cdn/shop/" in raw_src or "/products/" in raw_src):
            hi_res = get_high_res_image_url(raw_src)
            if hi_res not in image_urls:
                image_urls.append(hi_res)

    local_photo_paths: List[str] = []
    if download_photos and image_urls:
        local_photo_paths = download_product_images(session, image_urls, rank, handle, images_dir)

    overall_in_stock = len(in_stock_sizes) > 0
    return {
        "rank": rank,
        "product_id": None,
        "title": title,
        "handle": handle,
        "product_url": product_url,
        "vendor": "Helly Wild",
        "product_type": "Women's Jeans",
        "category": "Women's Jeans",
        "regular_price_lkr": price_val,
        "sale_price_lkr": price_val,
        "price_formatted": f"Rs {price_val:,.2f}",
        "installments": calculate_installments(price_val),
        "overall_in_stock": overall_in_stock,
        "overall_stock_status": "In Stock" if overall_in_stock else "Sold Out",
        "all_sizes": all_sizes,
        "in_stock_sizes": in_stock_sizes,
        "sold_out_sizes": sold_out_sizes,
        "variants_count": len(variants_list),
        "variants": variants_list,
        "specifications": specs,
        "description_text": desc_text,
        "description_html": desc_html,
        "tags": [],
        "published_at": "",
        "updated_at": "",
        "image_count": len(image_urls),
        "featured_image_url": image_urls[0] if image_urls else "",
        "image_urls": image_urls,
        "image_alt_texts": [title] * len(image_urls),
        "downloaded_photo_paths": local_photo_paths,
    }


def scrape_helly_wild_jeans(
    limit: int = 10,
    output_dir: str = "./helly_wild_scraped_data",
    download_photos: bool = True,
) -> List[Dict[str, Any]]:
    """Main scraper orchestrator for Helly Wild Women's Jeans category."""
    os.makedirs(output_dir, exist_ok=True)
    images_dir = os.path.join(output_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)

    scraped_products: List[Dict[str, Any]] = []

    print("=" * 78)
    print(" HELLY WILD WOMEN'S JEANS SCRAPER")
    print(f" Target Category : {COLLECTION_URL}")
    print(f" Products Limit  : First {limit} Products")
    print("=" * 78)

    # Step 1: Attempt fast & complete Shopify Collection JSON endpoint
    try:
        print(f"\n[*] Fetching collection feed from: {COLLECTION_JSON_URL}?limit={limit}")
        resp = session.get(COLLECTION_JSON_URL, params={"limit": limit}, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            raw_products = data.get("products", [])[:limit]
            if raw_products:
                print(f"[+] Found {len(raw_products)} products via Shopify Collection JSON feed.")
                for idx, prod in enumerate(raw_products, start=1):
                    print(f"\n[{idx}/{len(raw_products)}] Scraping: {prod.get('title')} (Rs {prod.get('variants', [{}])[0].get('price', 'N/A')})")
                    parsed = parse_shopify_product_json(
                        product_data=prod,
                        rank=idx,
                        session=session,
                        images_dir=images_dir,
                        download_photos=download_photos,
                    )
                    scraped_products.append(parsed)
                    time.sleep(0.4)
    except Exception as exc:
        print(f"[!] Collection JSON feed unavailable ({exc}). Switching to HTML collection parser...")

    # Step 2: If JSON feed returned fewer than requested or was blocked, parse HTML collection page
    if not scraped_products:
        print(f"\n[*] Scraping HTML collection page: {COLLECTION_URL}")
        resp = session.get(COLLECTION_URL, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        product_links: List[str] = []
        for a_tag in soup.select("a[href*='/products/']"):
            href = a_tag.get("href", "").split("?")[0]
            if not href:
                continue
            full_url = urljoin(BASE_URL, href)
            if full_url not in product_links:
                product_links.append(full_url)
            if len(product_links) >= limit:
                break

        print(f"[+] Found {len(product_links)} product links on category page.")
        for idx, purl in enumerate(product_links[:limit], start=1):
            print(f"\n[{idx}/{len(product_links[:limit])}] Scraping product page: {purl}")
            item = scrape_product_html_fallback(
                session=session,
                product_url=purl,
                rank=idx,
                images_dir=images_dir,
                download_photos=download_photos,
            )
            if item:
                scraped_products.append(item)
            time.sleep(0.5)

    # Step 3: Export all scraped data to JSON and CSV files
    export_all_formats(scraped_products, output_dir)
    return scraped_products


def export_all_formats(products: List[Dict[str, Any]], output_dir: str) -> None:
    """Save scraped products to JSON, Summary CSV, Variants Inventory CSV, and WooCommerce Import CSV."""
    # 1. Full JSON Export
    json_path = os.path.join(output_dir, "products_full_details.json")
    with open(json_path, "w", encoding="utf-8") as jf:
        json.dump(
            {
                "source_store": "Helly Wild (hellywild.lk)",
                "category_title": "Women's Jeans Sri Lanka | High Rise & Wide Leg Styles | Helly Wild – My Store",
                "category_url": COLLECTION_URL,
                "total_products_scraped": len(products),
                "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "products": products,
            },
            jf,
            indent=2,
            ensure_ascii=False,
        )

    # 2. Summary CSV Export (1 Row per Product with complete details)
    summary_csv_path = os.path.join(output_dir, "products_summary.csv")
    summary_fields = [
        "rank",
        "product_id",
        "title",
        "handle",
        "product_url",
        "regular_price_lkr",
        "sale_price_lkr",
        "mintpay_3x",
        "koko_3x",
        "payzy_4x",
        "overall_stock_status",
        "all_sizes",
        "in_stock_sizes",
        "sold_out_sizes",
        "material_fabric",
        "length_inches",
        "fit_and_rise",
        "sizing_note",
        "description_text",
        "image_count",
        "featured_image_url",
        "all_image_urls",
        "downloaded_photo_paths",
    ]
    with open(summary_csv_path, "w", newline="", encoding="utf-8-sig") as cf:
        writer = csv.DictWriter(cf, fieldnames=summary_fields)
        writer.writeheader()
        for p in products:
            writer.writerow({
                "rank": p["rank"],
                "product_id": p["product_id"],
                "title": p["title"],
                "handle": p["handle"],
                "product_url": p["product_url"],
                "regular_price_lkr": p["regular_price_lkr"],
                "sale_price_lkr": p["sale_price_lkr"],
                "mintpay_3x": p["installments"]["mintpay_3x"],
                "koko_3x": p["installments"]["koko_3x"],
                "payzy_4x": p["installments"]["payzy_4x"],
                "overall_stock_status": p["overall_stock_status"],
                "all_sizes": ", ".join(p["all_sizes"]),
                "in_stock_sizes": ", ".join(p["in_stock_sizes"]),
                "sold_out_sizes": ", ".join(p["sold_out_sizes"]),
                "material_fabric": p["specifications"]["material_fabric"],
                "length_inches": p["specifications"]["length_inches"],
                "fit_and_rise": p["specifications"]["fit_and_rise"],
                "sizing_note": p["specifications"]["sizing_note"],
                "description_text": p["description_text"],
                "image_count": p["image_count"],
                "featured_image_url": p["featured_image_url"],
                "all_image_urls": " | ".join(p["image_urls"]),
                "downloaded_photo_paths": " | ".join(p["downloaded_photo_paths"]),
            })

    # 3. Granular Variants Inventory CSV (1 Row per Size Variant)
    variants_csv_path = os.path.join(output_dir, "product_variants_inventory.csv")
    variant_fields = [
        "product_rank",
        "product_title",
        "product_handle",
        "variant_id",
        "sku",
        "size",
        "price_lkr",
        "compare_at_price_lkr",
        "stock_status",
        "in_stock",
        "product_url",
    ]
    with open(variants_csv_path, "w", newline="", encoding="utf-8-sig") as vf:
        writer = csv.DictWriter(vf, fieldnames=variant_fields)
        writer.writeheader()
        for p in products:
            for v in p["variants"]:
                writer.writerow({
                    "product_rank": p["rank"],
                    "product_title": p["title"],
                    "product_handle": p["handle"],
                    "variant_id": v["variant_id"],
                    "sku": v["sku"],
                    "size": v["size"],
                    "price_lkr": v["price_lkr"],
                    "compare_at_price_lkr": v["compare_at_price_lkr"],
                    "stock_status": v["stock_status"],
                    "in_stock": v["in_stock"],
                    "product_url": p["product_url"],
                })

    # 4. WordPress WooCommerce Direct Import CSV (WP Admin -> Products -> Import)
    woo_csv_path = os.path.join(output_dir, "woocommerce_import_ready.csv")
    woo_fields = [
        "Type",
        "SKU",
        "Name",
        "Published",
        "Short description",
        "Description",
        "In stock?",
        "Regular price",
        "Categories",
        "Images",
        "Parent",
        "Attribute 1 name",
        "Attribute 1 value(s)",
        "Attribute 1 visible",
        "Attribute 1 global",
    ]
    with open(woo_csv_path, "w", newline="", encoding="utf-8-sig") as wf:
        writer = csv.DictWriter(wf, fieldnames=woo_fields)
        writer.writeheader()
        for p in products:
            parent_sku = f"DY-JEANS-{p['rank']:02d}"
            short_desc = p["description_text"].split("\n")[0] if p["description_text"] else p["title"]
            writer.writerow({
                "Type": "variable" if p["all_sizes"] else "simple",
                "SKU": parent_sku,
                "Name": p["title"],
                "Published": 1,
                "Short description": short_desc,
                "Description": p["description_html"] or p["description_text"],
                "In stock?": 1 if p["overall_in_stock"] else 0,
                "Regular price": p["regular_price_lkr"] if not p["all_sizes"] else "",
                "Categories": "Women's Denim, Wide Leg",
                "Images": ", ".join(p["image_urls"]),
                "Parent": "",
                "Attribute 1 name": "Size",
                "Attribute 1 value(s)": ", ".join(p["all_sizes"]),
                "Attribute 1 visible": 1,
                "Attribute 1 global": 1,
            })
            # Write child variation rows for each size
            for v in p["variants"]:
                writer.writerow({
                    "Type": "variation",
                    "SKU": f"{parent_sku}-{v['size']}",
                    "Name": f"{p['title']} - Size {v['size']}",
                    "Published": 1,
                    "Short description": "",
                    "Description": "",
                    "In stock?": 1 if v["in_stock"] else 0,
                    "Regular price": v["price_lkr"],
                    "Categories": "",
                    "Images": "",
                    "Parent": parent_sku,
                    "Attribute 1 name": "Size",
                    "Attribute 1 value(s)": v["size"],
                    "Attribute 1 visible": "",
                    "Attribute 1 global": 1,
                })

    print("\n" + "=" * 78)
    print(" SCRAPING & EXPORT COMPLETE!")
    print(f" 1. Full JSON Details      : {json_path}")
    print(f" 2. Products Summary CSV   : {summary_csv_path}")
    print(f" 3. Size Variants CSV      : {variants_csv_path}")
    print(f" 4. WooCommerce Import CSV : {woo_csv_path}")
    print(f" 5. Downloaded Photos Dir  : {os.path.join(output_dir, 'images')}")
    print("=" * 78)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape the first N products (with full details and high-res photos) from Helly Wild Women's Jeans category."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of products to scrape from the category (default: 10)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="./helly_wild_scraped_data",
        help="Output folder path for JSON, CSVs, and downloaded product images",
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="Skip downloading image files to disk (still saves all high-res image URLs in JSON/CSV)",
    )
    args = parser.parse_args()

    try:
        scrape_helly_wild_jeans(
            limit=args.limit,
            output_dir=args.output,
            download_photos=not args.no_images,
        )
    except KeyboardInterrupt:
        print("\n[!] Scraping interrupted by user.")
        sys.exit(1)
    except Exception as err:
        print(f"\n[!] Error during scraping: {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()