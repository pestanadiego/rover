import time
import re
import traceback
from pathlib import Path

from dotenv import load_dotenv
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from rover.common import elapsed_seconds
from rover.keywords import selector as keyword_selector
from rover.keywords.store import KeywordStore
from rover.scraping.artifacts import save_scraper_error
from rover.scraping.config import load_scraper_config
from rover.scraping.selenium_driver import create_driver
from rover.scraping.sheet_keyword_writer import ScrapeKeywordSheetUpdater
from rover.pipeline_logging import configure_pipeline_logging, log_event


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOTENV_PATH = PROJECT_ROOT / ".env"
ASIN_RE = re.compile(r"\bB[A-Z0-9]{9}\b")
SELLERAMP_LOOKUP_URL = "https://sas.selleramp.com/r/sas/lookup"
AMAZON_HOME_URL = "https://www.amazon.com/"
SEARCH_BOX_SELECTORS = (
    "input#saslookup-search_term",
    "input#search_term",
    "input[name='search_term']",
    "input[type='search'][placeholder='Search Products']",
)
PRODUCT_ROW_SELECTOR = "div.relative.flex.flex-col.align-middle.border-l.border-panel-border"
EXPORT_BUTTON_SELECTOR = "#google-export-header button.btn-sheet-export2"
NEXT_PAGE_BUTTON_SELECTOR = "button[aria-label='Go to next page']"
NO_RESULTS_XPATH = "//p[contains(text(), 'No results were found')]"
TEMPORARY_SEARCH_ERROR_XPATH = (
    "//*[self::p or self::div or self::span]"
    "[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
    "\"couldn't complete your search\") and "
    "contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
    "'amazon.com')]"
)
LOGIN_EMAIL_SELECTORS = (
    "#login-form input#loginform-email",
    "#login-form input[name='LoginForm[email]']",
    "input[type='email']",
    "input[name='email']",
    "input[autocomplete='username']",
    "input#email",
)
LOGIN_PASSWORD_SELECTORS = (
    "#login-form input#loginform-password",
    "#login-form input[name='LoginForm[password]']",
    "input[type='password']",
    "input[name='password']",
    "input[autocomplete='current-password']",
    "input#password",
)
LOGIN_SUBMIT_XPATHS = (
    "//form[@id='login-form']//button[@name='login-button']",
    "//form[@id='login-form']//button[@type='submit']",
    "//button[@type='submit']",
    "//input[@type='submit']",
    "//button[contains(translate(normalize-space(.), 'LOGIN', 'login'), 'login')]",
    "//button[contains(translate(normalize-space(.), 'SIGN IN', 'sign in'), 'sign in')]",
    "//span[contains(translate(normalize-space(.), 'LOGIN', 'login'), 'login')]/ancestor::button[1]",
    "//span[contains(translate(normalize-space(.), 'SIGN IN', 'sign in'), 'sign in')]/ancestor::button[1]",
)

def parse_sales_velocity(sales_text):
    if not sales_text or 'unknown' in sales_text.lower(): return 0
    clean_text = sales_text.lower().replace('/mo', '').replace('+', '').replace(',', '').strip()
    if 'k' in clean_text:
        return int(float(clean_text.replace('k', '')) * 1000)
    match = re.search(r'\d+', clean_text)
    return int(match.group()) if match else 0

def parse_currency(cost_text):
    if not cost_text: return 0.0
    multiplier = -1 if '-' in cost_text else 1
    digits = re.sub(r'[^\d.]', '', cost_text)
    return float(digits) * multiplier if digits else 0.0

def parse_offers(offers_text):
    match = re.search(r'\d+', offers_text if offers_text else "")
    return int(match.group()) if match else 0

def find_asin(text):
    if not text:
        return None

    match = ASIN_RE.search(text.upper())
    if not match:
        return None

    return match.group()


def selected_keyword_text(selected_keyword):
    if isinstance(selected_keyword, str):
        keyword = selected_keyword.strip()
        return keyword or None

    return str(selected_keyword.get("keyword", "")).strip() or None


def selected_keyword_metadata(selected_keyword, fallback_scheduler_run_id=None):
    if not isinstance(selected_keyword, dict):
        return {
            "scheduler_run_id": fallback_scheduler_run_id,
            "scheduled_keyword_id": None,
            "priority_score": None,
            "selection_bucket": None,
        }

    return {
        "scheduler_run_id": selected_keyword.get("scheduler_run_id") or fallback_scheduler_run_id,
        "scheduled_keyword_id": selected_keyword.get("scheduled_keyword_id"),
        "priority_score": selected_keyword.get("priority_score"),
        "selection_bucket": selected_keyword.get("selection_bucket") or selected_keyword.get("bucket"),
    }


def normalize_selected_keywords(selected_keywords):
    if selected_keywords is None:
        return []

    if isinstance(selected_keywords, str):
        return [selected_keywords]

    if isinstance(selected_keywords, dict):
        return list(selected_keywords.get("keywords", []))

    return list(selected_keywords)


def select_keywords_with_scheduler(keyword_store=None, global_winner_limit=None):
    selector_result = keyword_selector.select_keywords_for_run(
        keyword_store=keyword_store,
        project_root=PROJECT_ROOT,
        global_winner_limit=global_winner_limit,
    )
    selected_keywords = normalize_selected_keywords(selector_result)
    scheduler_run_id = selector_result.get("scheduler_run_id")
    log_event(
        "scraper_keyword_selector_completed",
        stage="Scrape keywords",
        selector="select_keywords_for_run",
        scheduler_run_id=scheduler_run_id,
        selected_count=len(selected_keywords),
    )
    return selected_keywords, scheduler_run_id


def selleramp_loop_result(
    winners_found,
    maxed_out,
    sheet_rows_updated,
    error_message,
    duplicate_asins_skipped,
    return_summary,
    products_seen=0,
):
    if return_summary:
        return {
            "winners_found": winners_found,
            "maxed_out": maxed_out,
            "sheet_rows_updated": sheet_rows_updated,
            "products_seen": products_seen,
            "duplicate_asins_skipped": duplicate_asins_skipped,
            "error_message": error_message,
        }

    return winners_found, maxed_out, sheet_rows_updated, error_message


def wait_for_search_result_state(driver, timeout_seconds):
    try:
        return WebDriverWait(driver, timeout_seconds).until(current_search_result_state)
    except TimeoutException:
        return "timeout"


def current_search_result_state(driver):
    if elements_found(driver, By.CSS_SELECTOR, PRODUCT_ROW_SELECTOR):
        return "results"

    if element_exists(driver, By.XPATH, NO_RESULTS_XPATH):
        return "no_results"

    if element_exists(driver, By.XPATH, TEMPORARY_SEARCH_ERROR_XPATH):
        return "temporary_search_error"

    return False


def wait_for_sheet_row_count_to_increase(sheet_updater, previous_count, timeout_seconds):
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        if sheet_updater.row_count() > previous_count:
            return True
        time.sleep(0.5)

    return False


def ensure_selleramp_ready(driver, wait, scraper_config):
    if search_input_visible(driver):
        return

    if not login_form_present(driver):
        raise RuntimeError(
            "SellerAmp is not ready. Search box was not found and no login form was detected."
        )

    perform_selleramp_login(driver, wait, scraper_config)
    wait_for_search_input(driver, wait)


def prepare_selleramp_search_page(driver, wait, scraper_config, keyword, reason):
    log_event(
        "scraper_selleramp_reset_started",
        stage="Scrape keywords",
        keyword=keyword,
        reason=reason,
        current_url=safe_driver_url(driver),
        title=safe_driver_title(driver),
    )
    switch_to_selleramp_tab_or_open_lookup(driver)
    switch_to_default_content(driver)
    driver.get(SELLERAMP_LOOKUP_URL)
    wait_for_document_ready(driver, scraper_config.browser.element_wait_timeout_seconds)
    ensure_selleramp_ready(driver, wait, scraper_config)
    search_input = wait_for_search_input(driver, wait)
    log_event(
        "scraper_selleramp_reset_completed",
        stage="Scrape keywords",
        keyword=keyword,
        reason=reason,
        current_url=safe_driver_url(driver),
        title=safe_driver_title(driver),
    )
    return search_input


def switch_to_selleramp_tab_or_open_lookup(driver):
    try:
        switch_to_selleramp_tab(driver)
        return
    except RuntimeError:
        driver.execute_script("window.open(arguments[0], '_blank');", SELLERAMP_LOOKUP_URL)
        driver.switch_to.window(driver.window_handles[-1])


def switch_to_default_content(driver):
    try:
        driver.switch_to.default_content()
    except Exception:
        return


def wait_for_document_ready(driver, timeout_seconds):
    try:
        WebDriverWait(driver, timeout_seconds).until(
            lambda current_driver: current_driver.execute_script(
                "return document.readyState"
            )
            in ("interactive", "complete")
        )
    except TimeoutException:
        return


def search_input_visible(driver):
    return first_visible_element(driver, By.CSS_SELECTOR, SEARCH_BOX_SELECTORS) is not None


def wait_for_search_input(driver, wait):
    return wait.until(
        lambda current_driver: first_enabled_element(
            current_driver,
            By.CSS_SELECTOR,
            SEARCH_BOX_SELECTORS,
        )
    )


def is_search_input_usable(element):
    try:
        return element is not None and element.is_displayed() and element.is_enabled()
    except Exception:
        return False


def login_form_present(driver):
    form = first_visible_element(driver, By.CSS_SELECTOR, ("form#login-form",))
    if not form:
        return False

    return bool(first_visible_element(driver, By.CSS_SELECTOR, LOGIN_EMAIL_SELECTORS)) and bool(
        first_visible_element(driver, By.CSS_SELECTOR, LOGIN_PASSWORD_SELECTORS)
    )


def perform_selleramp_login(driver, wait, scraper_config):
    if not scraper_config.auth.email or not scraper_config.auth.password:
        raise RuntimeError(
            "SellerAmp login required, but SELLERAMP_EMAIL or SELLERAMP_PASSWORD is missing."
        )

    print("SellerAmp login required. Attempting sign-in...")
    email_input = wait_for_first_visible(driver, wait, By.CSS_SELECTOR, LOGIN_EMAIL_SELECTORS)
    password_input = wait_for_first_visible(driver, wait, By.CSS_SELECTOR, LOGIN_PASSWORD_SELECTORS)
    submit_button = wait_for_first_visible(driver, wait, By.XPATH, LOGIN_SUBMIT_XPATHS)

    clear_and_type(email_input, scraper_config.auth.email)
    clear_and_type(password_input, scraper_config.auth.password)
    driver.execute_script("arguments[0].click();", submit_button)
    time.sleep(scraper_config.auth.post_login_wait_seconds)


def clear_and_type(element, value):
    element.clear()
    element.send_keys(value)


def wait_for_first_visible(driver, wait, by, selectors):
    for selector in selectors:
        try:
            return wait.until(EC.visibility_of_element_located((by, selector)))
        except TimeoutException:
            continue

    raise TimeoutException(f"Could not find a visible element for selectors: {selectors}")


def first_visible_element(driver, by, selectors):
    for selector in selectors:
        try:
            elements = driver.find_elements(by, selector)
        except Exception:
            continue

        for element in elements:
            try:
                if element.is_displayed():
                    return element
            except Exception:
                continue

    return None


def first_enabled_element(driver, by, selectors):
    for selector in selectors:
        try:
            elements = driver.find_elements(by, selector)
        except Exception:
            continue

        for element in elements:
            try:
                if element.is_displayed() and element.is_enabled():
                    return element
            except Exception:
                continue

    return None


def elements_found(driver, by, selector):
    try:
        return len(driver.find_elements(by, selector)) > 0
    except Exception:
        return False


def element_exists(driver, by, selector):
    try:
        elements = driver.find_elements(by, selector)
    except Exception:
        return False

    return any(safely_displayed(element) for element in elements)


def safely_displayed(element):
    try:
        return element.is_displayed()
    except Exception:
        return False


def switch_to_selleramp_tab(driver):
    seen_tabs = []

    for window_handle in driver.window_handles:
        driver.switch_to.window(window_handle)
        title = safe_driver_title(driver)
        current_url = safe_driver_url(driver)
        seen_tabs.append({"title": title, "url": current_url})

        if is_selleramp_page(title, current_url):
            return

    formatted_tabs = ", ".join(
        f"title={tab['title']!r} url={tab['url']!r}"
        for tab in seen_tabs
    )
    raise RuntimeError(
        "Could not find a SellerAmp tab. "
        f"Open tabs were: {formatted_tabs or 'none'}"
    )


def is_selleramp_page(title, current_url):
    url = (current_url or "").lower()
    page_title = (title or "").lower()

    if "selleramp.com" in url:
        return True

    if "selleramp" in page_title or "sas" in page_title:
        return True

    return False


def safe_driver_title(driver):
    try:
        return driver.title
    except Exception:
        return ""


def safe_driver_url(driver):
    try:
        return driver.current_url
    except Exception:
        return ""


def recover_temporary_search_error(driver, wait, scraper_config, keyword, attempt):
    print("     [!] SellerAmp reported a temporary Amazon search issue. Recovering...")
    log_event(
        "scraper_temporary_search_error_recovery_started",
        stage="Scrape keywords",
        level="WARNING",
        keyword=keyword,
        attempt=attempt,
        current_url=safe_driver_url(driver),
    )
    original_handle = safe_window_handle(driver)
    handles_before = list(driver.window_handles)

    driver.execute_script("window.open(arguments[0], '_blank');", AMAZON_HOME_URL)
    amazon_handle = newest_window_handle(driver, handles_before)
    if not amazon_handle:
        raise RuntimeError("Could not open Amazon recovery tab.")

    driver.switch_to.window(amazon_handle)
    wait_for_document_ready(driver, scraper_config.browser.element_wait_timeout_seconds)
    time.sleep(2)
    if len(driver.window_handles) > 1:
        driver.close()

    switch_back_to_selleramp(driver, original_handle)
    search_input = prepare_selleramp_search_page(
        driver,
        wait,
        scraper_config,
        keyword,
        reason="temporary_search_error",
    )
    log_event(
        "scraper_temporary_search_error_recovery_completed",
        stage="Scrape keywords",
        keyword=keyword,
        attempt=attempt,
        current_url=safe_driver_url(driver),
    )
    return search_input


def safe_window_handle(driver):
    try:
        return driver.current_window_handle
    except Exception:
        return None


def newest_window_handle(driver, handles_before):
    current_handles = list(driver.window_handles)
    new_handles = [handle for handle in current_handles if handle not in handles_before]
    if new_handles:
        return new_handles[-1]
    return None


def switch_back_to_selleramp(driver, preferred_handle):
    if preferred_handle in list(driver.window_handles):
        driver.switch_to.window(preferred_handle)
        return

    switch_to_selleramp_tab(driver)

def run_selleramp_export_loop(
    keyword,
    is_first_keyword=False,
    sheet_updater=None,
    keyword_store=None,
    return_summary=False,
    driver=None,
    scraper_config=None,
):
    scraper_config = scraper_config or load_scraper_config()
    managed_driver = None
    if driver is None:
        managed_driver = create_driver(scraper_config)
        driver = managed_driver.driver

    wait = WebDriverWait(driver, scraper_config.browser.element_wait_timeout_seconds)
    winners_found = 0
    sheet_rows_updated = 0
    products_seen = 0
    duplicate_asins_skipped = 0
    maxed_out = False
    current_page = 1
    current_product_index = None
    started_at = time.monotonic()

    try:
        log_event(
            "scraper_keyword_loop_started",
            stage="Scrape keywords",
            keyword=keyword,
            is_first_keyword=is_first_keyword,
            browser_mode=scraper_config.browser.mode,
            target_winners=scraper_config.scrape.target_winners_per_keyword,
            max_pages_to_scrape=scraper_config.scrape.max_pages_to_scrape,
            start_page=scraper_config.scrape.start_page,
            min_sales_per_month=scraper_config.filters.min_sales_per_month,
            min_sellers=scraper_config.filters.min_sellers,
            min_cost=scraper_config.filters.min_cost,
            skip_amazon=scraper_config.filters.skip_amazon,
        )
        print(f"Initially attached to tab: {safe_driver_title(driver)}")
        log_event(
            "scraper_browser_attached",
            stage="Scrape keywords",
            keyword=keyword,
            title=safe_driver_title(driver),
            url=safe_driver_url(driver),
        )
        search_input = prepare_selleramp_search_page(
            driver,
            wait,
            scraper_config,
            keyword,
            reason="keyword_start",
        )
        log_event(
            "scraper_selleramp_ready",
            stage="Scrape keywords",
            keyword=keyword,
            title=safe_driver_title(driver),
            url=safe_driver_url(driver),
        )

        target_winners = scraper_config.scrape.target_winners_per_keyword
        max_pages_to_scrape = scraper_config.scrape.max_pages_to_scrape
        start_page = scraper_config.scrape.start_page
        skip_amazon = scraper_config.filters.skip_amazon
        min_sales_per_month = scraper_config.filters.min_sales_per_month
        min_sellers = scraper_config.filters.min_sellers
        min_cost = scraper_config.filters.min_cost

        AMZ_BADGE = "div.amz" 
        OFFERS_BOX = ".//p[contains(text(), 'Offers:')]" 
        EST_SALES_BOX = ".//p[contains(text(), 'Est. Sales')]/following-sibling::div"
        MAX_COST_BOX = ".//p[contains(text(), 'Max Cost')]/following-sibling::div"

        # execute search
        retries = 0
        search_successful = False

        while retries <= scraper_config.retries.no_results_max_retries:
            print(f"\nStep 1: Searching for keyword '{keyword}' (Attempt {retries + 1})...")
            log_event(
                "scraper_search_attempt_started",
                stage="Scrape keywords",
                keyword=keyword,
                attempt=retries + 1,
                max_retries=scraper_config.retries.no_results_max_retries,
            )
            if not is_search_input_usable(search_input):
                search_input = wait_for_search_input(driver, wait)

            search_input.clear()
            search_input.send_keys(keyword)
            search_input.send_keys(Keys.RETURN)
            print("Search executed. Waiting for results...")
            search_state = wait_for_search_result_state(
                driver,
                scraper_config.timing.search_results_wait_seconds,
            )

            if search_state == "results":
                search_successful = True
                log_event(
                    "scraper_search_results_ready",
                    stage="Scrape keywords",
                    keyword=keyword,
                    attempt=retries + 1,
                )
                break

            if search_state == "no_results":
                if retries < scraper_config.retries.no_results_max_retries:
                    cooldown = scraper_config.retries.no_results_cooldown_seconds
                    print(f"     [!] No results found. Initiating {cooldown}-second cooldown before retry...")
                    log_event(
                        "scraper_search_no_results_retry",
                        stage="Scrape keywords",
                        level="WARNING",
                        keyword=keyword,
                        attempt=retries + 1,
                        cooldown_seconds=cooldown,
                    )
                    time.sleep(cooldown)
                    search_input = prepare_selleramp_search_page(
                        driver,
                        wait,
                        scraper_config,
                        keyword,
                        reason="no_results_retry",
                    )
                    retries += 1
                    continue

                print(f"     [!] Max retries reached for '{keyword}'. Moving on.")
                maxed_out = True
                log_event(
                    "scraper_search_max_retries",
                    stage="Scrape keywords",
                    level="WARNING",
                    keyword=keyword,
                    attempts=retries + 1,
                    search_state=search_state,
                )
                return selleramp_loop_result(
                    winners_found,
                    maxed_out,
                    sheet_rows_updated,
                    None,
                    duplicate_asins_skipped,
                    return_summary,
                    products_seen=products_seen,
                )

            if search_state == "temporary_search_error":
                if retries < scraper_config.retries.no_results_max_retries:
                    search_input = recover_temporary_search_error(
                        driver,
                        wait,
                        scraper_config,
                        keyword,
                        attempt=retries + 1,
                    )
                    retries += 1
                    continue

                raise RuntimeError(
                    "SellerAmp temporary search error did not recover "
                    f"after {retries + 1} attempt(s) for keyword '{keyword}'."
                )

            if search_state == "timeout":
                if retries < scraper_config.retries.no_results_max_retries:
                    log_event(
                        "scraper_search_state_timeout_retry",
                        stage="Scrape keywords",
                        level="WARNING",
                        keyword=keyword,
                        attempt=retries + 1,
                        timeout_seconds=scraper_config.timing.search_results_wait_seconds,
                        current_url=safe_driver_url(driver),
                    )
                    search_input = prepare_selleramp_search_page(
                        driver,
                        wait,
                        scraper_config,
                        keyword,
                        reason="search_state_timeout",
                    )
                    retries += 1
                    continue

                raise RuntimeError(
                    "SellerAmp search timed out waiting for products, no-results, "
                    "or temporary-error state "
                    f"after {retries + 1} attempt(s) for keyword '{keyword}'."
                )

            raise RuntimeError(f"Unexpected SellerAmp search state: {search_state!r}")

        if not search_successful:
            return selleramp_loop_result(
                winners_found,
                maxed_out,
                sheet_rows_updated,
                None,
                duplicate_asins_skipped,
                return_summary,
                products_seen=products_seen,
            )

        # skip to starting page
        if start_page > 1:
            print(f"\nStep 2: Fast-forwarding to Page {start_page}...")
            while current_page < start_page:
                try:
                    next_button = driver.find_element(By.CSS_SELECTOR, NEXT_PAGE_BUTTON_SELECTOR)
                    if next_button.get_attribute("aria-disabled") == "true" or next_button.get_attribute("disabled"):
                        print(f"Reached the last page while skipping. Starting on Page {current_page}.")
                        break
                        
                    driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", next_button)
                    time.sleep(0.5)
                    driver.execute_script("arguments[0].click();", next_button)
                    
                    print(f"Skipping page {current_page}...")
                    current_page += 1
                    time.sleep(scraper_config.timing.next_page_wait_seconds)
                except NoSuchElementException:
                    print(f"No 'Next' button found. Starting on Page {current_page}.")
                    break

        # main loop
        pages_scraped = 0
        
        while True:
            pages_scraped += 1
            print(f"\n--- Scraping Page {current_page} (Scrape block {pages_scraped}/{max_pages_to_scrape}) ---")
            page_started_at = time.monotonic()
            log_event(
                "scraper_page_started",
                stage="Scrape keywords",
                keyword=keyword,
                page=current_page,
                scrape_block=pages_scraped,
                max_pages_to_scrape=max_pages_to_scrape,
            )
            
            print("Waiting for product rows to render...")
            wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, PRODUCT_ROW_SELECTOR)))
            
            product_count = len(driver.find_elements(By.CSS_SELECTOR, PRODUCT_ROW_SELECTOR))
            products_seen += product_count
            print(f"Found {product_count} products on this page.")
            log_event(
                "scraper_page_products_loaded",
                stage="Scrape keywords",
                keyword=keyword,
                page=current_page,
                scrape_block=pages_scraped,
                product_count=product_count,
                products_seen=products_seen,
            )
            
            if product_count == 0: break

            for i in range(product_count):
                current_product_index = i + 1
                print(f"\n  -> Evaluating product {i + 1}/{product_count}...")
                product_started_at = time.monotonic()
                log_event(
                    "scraper_product_evaluation_started",
                    stage="Scrape keywords",
                    level="DEBUG",
                    keyword=keyword,
                    page=current_page,
                    product_index=current_product_index,
                    product_count=product_count,
                )
                
                products = driver.find_elements(By.CSS_SELECTOR, PRODUCT_ROW_SELECTOR)
                current_product = products[i]

                # force scroll for lazy loading
                driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", current_product)
                
                # wait up to 5 seconds for the data to hydrate
                data_loaded = False
                for _ in range(10): 
                    try:
                        current_product.find_element(By.XPATH, OFFERS_BOX)
                        data_loaded = True
                        break
                    except NoSuchElementException:
                        time.sleep(0.5)
                        
                if not data_loaded:
                    print("     [SKIP] Product data failed to load in time (Timeout).")
                    log_scraper_product_skip(
                        keyword,
                        current_page,
                        current_product_index,
                        "data_load_timeout",
                    )
                    continue

                try:
                    if skip_amazon:
                        amz_badges = current_product.find_elements(By.CSS_SELECTOR, AMZ_BADGE)
                        if len(amz_badges) > 0:
                            print("     [SKIP] Amazon is on this listing.")
                            log_scraper_product_skip(
                                keyword,
                                current_page,
                                current_product_index,
                                "amazon_on_listing",
                            )
                            continue
                    
                    try:
                        offers_element = current_product.find_element(By.XPATH, OFFERS_BOX)
                        total_offers = parse_offers(offers_element.text)
                    except NoSuchElementException:
                        print("     [SKIP] No offers data found.")
                        log_scraper_product_skip(
                            keyword,
                            current_page,
                            current_product_index,
                            "missing_offers",
                        )
                        continue
                        
                    if total_offers < min_sellers:
                        print(f"     [SKIP] Potential IP Trap (Only {total_offers} offers).")
                        log_scraper_product_skip(
                            keyword,
                            current_page,
                            current_product_index,
                            "too_few_offers",
                            offers=total_offers,
                            min_sellers=min_sellers,
                        )
                        continue

                    try:
                        sales_element = current_product.find_element(By.XPATH, EST_SALES_BOX)
                        est_sales = parse_sales_velocity(sales_element.text)
                    except NoSuchElementException:
                        print("     [SKIP] No Est. Sales data available on this listing.")
                        log_scraper_product_skip(
                            keyword,
                            current_page,
                            current_product_index,
                            "missing_estimated_sales",
                        )
                        continue
                        
                    if est_sales < min_sales_per_month:
                        print(f"     [SKIP] Sales velocity too low ({est_sales}/mo).")
                        log_scraper_product_skip(
                            keyword,
                            current_page,
                            current_product_index,
                            "sales_velocity_too_low",
                            estimated_sales=est_sales,
                            min_sales_per_month=min_sales_per_month,
                        )
                        continue

                    try:
                        cost_element = current_product.find_element(By.XPATH, MAX_COST_BOX)
                        max_cost = parse_currency(cost_element.text)
                    except NoSuchElementException:
                        print("     [SKIP] No Max Cost data available on this listing.")
                        log_scraper_product_skip(
                            keyword,
                            current_page,
                            current_product_index,
                            "missing_max_cost",
                        )
                        continue
                        
                    if max_cost < min_cost:
                        print(f"     [SKIP] Max cost too low or negative (${max_cost}).")
                        log_scraper_product_skip(
                            keyword,
                            current_page,
                            current_product_index,
                            "max_cost_too_low",
                            max_cost=max_cost,
                            min_cost=min_cost,
                        )
                        continue
                        
                    print(f"     [PASS] Product meets criteria! (Sales: {est_sales}, Offers: {total_offers}, Max Cost: ${max_cost})")
                    log_event(
                        "scraper_product_filter_passed",
                        stage="Scrape keywords",
                        keyword=keyword,
                        page=current_page,
                        product_index=current_product_index,
                        elapsed_seconds=elapsed_seconds(product_started_at),
                        estimated_sales=est_sales,
                        offers=total_offers,
                        max_cost=max_cost,
                    )

                except Exception as e:
                    print(f"     [!] Unexpected error parsing product data: {e}. Skipping.")
                    log_event(
                        "scraper_product_parse_failed",
                        stage="Scrape keywords",
                        level="ERROR",
                        keyword=keyword,
                        page=current_page,
                        product_index=current_product_index,
                        error_type=type(e).__name__,
                        error=str(e),
                    )
                    save_scraper_error(
                        driver,
                        scraper_config,
                        keyword=keyword,
                        error=e,
                        page_number=current_page,
                        product_index=current_product_index,
                    )
                    continue

                print("     Clicking product to load sidebar data...")
                product_asin = find_asin(current_product.text)

                if keyword_store and keyword_store.asin_exists(product_asin):
                    print(f"     [SKIP] ASIN already exists in products: {product_asin}")
                    duplicate_asins_skipped += 1
                    log_scraper_product_skip(
                        keyword,
                        current_page,
                        current_product_index,
                        "duplicate_asin",
                        asin=product_asin,
                        duplicate_asins_skipped=duplicate_asins_skipped,
                    )
                    continue

                sheet_row_count_before_export = sheet_updater.row_count()
                log_event(
                    "scraper_product_export_started",
                    stage="Scrape keywords",
                    keyword=keyword,
                    page=current_page,
                    product_index=current_product_index,
                    asin=product_asin,
                    sheet_row_count_before_export=sheet_row_count_before_export,
                )
                driver.execute_script("arguments[0].click();", current_product)
                
                time.sleep(scraper_config.timing.sidebar_wait_seconds)
                
                try:
                    wait.until(EC.frame_to_be_available_and_switch_to_it((By.ID, "sasFrame")))
                    wait.until(EC.frame_to_be_available_and_switch_to_it((By.ID, "appFrame")))
                    time.sleep(scraper_config.timing.iframe_inner_wait_seconds)
                    
                    export_button = wait.until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, EXPORT_BUTTON_SELECTOR))
                    )
                    driver.execute_script("arguments[0].click();", export_button)
                    
                    print("     Verifying Google Sheets export...")
                    check_icon = export_button.find_element(By.CSS_SELECTOR, "i.progress-check")
                    WebDriverWait(driver, 10).until(EC.visibility_of(check_icon))
                    wait_for_sheet_row_count_to_increase(
                        sheet_updater,
                        sheet_row_count_before_export,
                        scraper_config.timing.sheet_row_wait_seconds,
                    )

                    print("     Filling Scrape Keyword in Google Sheet...")
                    updated_row = sheet_updater.fill_scrape_keyword(
                        keyword=keyword,
                        asin=product_asin,
                        min_row_number=sheet_row_count_before_export + 1,
                    )

                    if not updated_row:
                        raise RuntimeError("Could not find the exported row in Google Sheets.")

                    sheet_rows_updated += 1
                    print(f"     [SUCCESS] Wrote Scrape Keyword to sheet row {updated_row}.")
                    
                    winners_found += 1
                    print(f"     [SUCCESS] Exported winning product {winners_found}/{target_winners}!")
                    log_event(
                        "scraper_product_export_completed",
                        stage="Scrape keywords",
                        keyword=keyword,
                        page=current_page,
                        product_index=current_product_index,
                        asin=product_asin,
                        elapsed_seconds=elapsed_seconds(product_started_at),
                        updated_row=updated_row,
                        sheet_rows_updated=sheet_rows_updated,
                        winners_found=winners_found,
                        target_winners=target_winners,
                    )
                    time.sleep(1)
                    
                except TimeoutException as error:
                    print("     [!] Export failed or timed out. Checkmark never appeared. Skipping.")
                    log_event(
                        "scraper_product_export_failed",
                        stage="Scrape keywords",
                        level="ERROR",
                        keyword=keyword,
                        page=current_page,
                        product_index=current_product_index,
                        asin=product_asin,
                        error_type=type(error).__name__,
                        error=str(error),
                    )
                    save_scraper_error(
                        driver,
                        scraper_config,
                        keyword=keyword,
                        error=error,
                        page_number=current_page,
                        product_index=current_product_index,
                    )
                    
                finally:
                    driver.switch_to.default_content()
                    time.sleep(0.5)

                if winners_found >= target_winners:
                    break 

            if winners_found >= target_winners:
                print(f"\n[COMPLETE] Found {target_winners} winning products for '{keyword}'. Stopping scrape.")
                log_event(
                    "scraper_keyword_target_reached",
                    stage="Scrape keywords",
                    keyword=keyword,
                    winners_found=winners_found,
                    target_winners=target_winners,
                )
                break
                
            if pages_scraped >= max_pages_to_scrape:
                print(f"\n[COMPLETE] Reached the maximum limit of {max_pages_to_scrape} scraped pages for '{keyword}'.")
                log_event(
                    "scraper_keyword_page_limit_reached",
                    stage="Scrape keywords",
                    keyword=keyword,
                    page=current_page,
                    pages_scraped=pages_scraped,
                    max_pages_to_scrape=max_pages_to_scrape,
                )
                break

            log_event(
                "scraper_page_completed",
                stage="Scrape keywords",
                keyword=keyword,
                page=current_page,
                elapsed_seconds=elapsed_seconds(page_started_at),
                products_seen=products_seen,
                winners_found=winners_found,
            )
            print("\nLooking for the 'Next' page button...")
            try:
                next_button = driver.find_element(By.CSS_SELECTOR, NEXT_PAGE_BUTTON_SELECTOR)
                if next_button.get_attribute("aria-disabled") == "true" or next_button.get_attribute("disabled"):
                    print(f"[COMPLETE] Reached the final page for '{keyword}'. No more results.")
                    log_event(
                        "scraper_keyword_final_page_reached",
                        stage="Scrape keywords",
                        keyword=keyword,
                        page=current_page,
                    )
                    break
                
                driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", next_button)
                time.sleep(0.5)
                driver.execute_script("arguments[0].click();", next_button)
                print("Moving to the next page. Waiting for transition...")
                current_page += 1
                time.sleep(scraper_config.timing.next_page_wait_seconds)
                
            except NoSuchElementException:
                print(f"[COMPLETE] No 'Next' button found. End of results for '{keyword}'.")
                log_event(
                    "scraper_keyword_next_button_missing",
                    stage="Scrape keywords",
                    keyword=keyword,
                    page=current_page,
                )
                break

        log_event(
            "scraper_keyword_loop_completed",
            stage="Scrape keywords",
            keyword=keyword,
            elapsed_seconds=elapsed_seconds(started_at),
            winners_found=winners_found,
            maxed_out=maxed_out,
            sheet_rows_updated=sheet_rows_updated,
            products_seen=products_seen,
            duplicate_asins_skipped=duplicate_asins_skipped,
        )
        return selleramp_loop_result(
            winners_found,
            maxed_out,
            sheet_rows_updated,
            None,
            duplicate_asins_skipped,
            return_summary,
            products_seen=products_seen,
        )

    except Exception as e:
        print(f"\n[!] A FATAL ERROR OCCURRED ON KEYWORD '{keyword}': {type(e).__name__}")
        print(f"Error Message: {e}")
        traceback.print_exc()
        log_event(
            "scraper_keyword_loop_failed",
            stage="Scrape keywords",
            level="ERROR",
            keyword=keyword,
            elapsed_seconds=elapsed_seconds(started_at),
            page=current_page,
            product_index=current_product_index,
            winners_found=winners_found,
            products_seen=products_seen,
            error_type=type(e).__name__,
            error=str(e),
            traceback=traceback.format_exc(),
        )
        save_scraper_error(
            driver,
            scraper_config,
            keyword=keyword,
            error=e,
            page_number=current_page,
            product_index=current_product_index,
        )
        return selleramp_loop_result(
            winners_found,
            maxed_out,
            sheet_rows_updated,
            str(e),
            duplicate_asins_skipped,
            return_summary,
            products_seen=products_seen,
        )

    finally:
        print(f"\nFinished processing keyword: {keyword}")
        if managed_driver is not None:
            managed_driver.quit()

def scrape_keywords(
    keywords,
    keyword_store,
    sheet_updater,
    global_winner_limit=None,
    scheduler_run_id=None,
    scraper_config=None,
):
    stage_started_at = time.monotonic()
    scraper_config = scraper_config or load_scraper_config()
    if global_winner_limit is None:
        global_winner_limit = scraper_config.scrape.global_winner_limit

    selected_keywords = normalize_selected_keywords(keywords)
    summary = {
        "scheduler_run_id": scheduler_run_id,
        "keywords_requested": len(selected_keywords),
        "keywords_attempted": 0,
        "keywords_skipped": 0,
        "keywords_completed": 0,
        "products_seen": 0,
        "total_winners_found": 0,
        "sheet_rows_updated": 0,
        "duplicate_asins_skipped": 0,
        "maxed_out_keywords": 0,
        "errors": [],
        "stopped_reason": None,
        "keyword_results": [],
    }
    log_event(
        "scraper_stage_started",
        stage="Scrape keywords",
        scheduler_run_id=scheduler_run_id,
        keywords_requested=len(selected_keywords),
        selected_keywords=[
            selected_keyword_text(selected_keyword)
            for selected_keyword in selected_keywords[:25]
        ],
        global_winner_limit=global_winner_limit,
        target_winners_per_keyword=scraper_config.scrape.target_winners_per_keyword,
        browser_mode=scraper_config.browser.mode,
    )

    if not selected_keywords:
        summary["stopped_reason"] = "no_keywords_selected"
        print("No keywords selected for scraping.")
        log_event(
            "scraper_stage_skipped",
            stage="Scrape keywords",
            level="WARNING",
            reason="no_keywords_selected",
            elapsed_seconds=elapsed_seconds(stage_started_at),
        )
        return summary

    total_winners_found = 0
    maxed_out_keywords_count = 0
    is_first = True

    print("Starting multi-keyword scraping sequence...")
    log_event(
        "scraper_driver_starting",
        stage="Scrape keywords",
        browser_mode=scraper_config.browser.mode,
    )
    managed_driver = create_driver(scraper_config)
    log_event(
        "scraper_driver_started",
        stage="Scrape keywords",
        browser_mode=scraper_config.browser.mode,
        window_count=len(getattr(managed_driver.driver, "window_handles", []) or []),
    )

    try:
        for selected_keyword in selected_keywords:
            keyword = selected_keyword_text(selected_keyword)

            if not keyword:
                print(f"\n[SKIP] Could not determine keyword text from selected item: {selected_keyword!r}")
                log_event(
                    "scraper_keyword_skipped",
                    stage="Scrape keywords",
                    level="WARNING",
                    reason="invalid_keyword",
                    selected_keyword=repr(selected_keyword),
                )
                summary["keywords_skipped"] += 1
                summary["keyword_results"].append(
                    {
                        "keyword": None,
                        "status": "invalid_keyword",
                        "selected_keyword": repr(selected_keyword),
                    }
                )
                continue

            if keyword_store.should_skip_keyword(keyword):
                print(f"\n[SKIP] Keyword is retired: '{keyword}'")
                log_event(
                    "scraper_keyword_skipped",
                    stage="Scrape keywords",
                    level="WARNING",
                    keyword=keyword,
                    reason="retired",
                )
                summary["keywords_skipped"] += 1
                summary["keyword_results"].append(
                    {
                        "keyword": keyword,
                        "status": "skipped_retired",
                        "winners_found": 0,
                        "products_seen": 0,
                        "sheet_rows_updated": 0,
                        "duplicate_asins_skipped": 0,
                    }
                )
                continue

            if total_winners_found >= global_winner_limit:
                print("\n==========================================")
                print(f"[SUCCESS] Hit global target of {global_winner_limit} total winners. Shutting down.")
                print("==========================================")
                summary["stopped_reason"] = "global_winner_limit"
                log_event(
                    "scraper_global_winner_limit_reached",
                    stage="Scrape keywords",
                    total_winners_found=total_winners_found,
                    global_winner_limit=global_winner_limit,
                )
                break

            print("\n==========================================")
            print(f"INITIATING SEARCH FOR: '{keyword}'")
            print("==========================================")

            metadata = selected_keyword_metadata(selected_keyword, scheduler_run_id)
            keyword_run_id = keyword_store.start_run(
                keyword,
                scheduler_run_id=metadata["scheduler_run_id"],
                scheduled_keyword_id=metadata["scheduled_keyword_id"],
                priority_score=metadata["priority_score"],
                selection_bucket=metadata["selection_bucket"],
            )
            summary["keywords_attempted"] += 1
            log_event(
                "scraper_keyword_run_started",
                stage="Scrape keywords",
                keyword=keyword,
                keyword_run_id=keyword_run_id,
                scheduler_run_id=metadata["scheduler_run_id"],
                scheduled_keyword_id=metadata["scheduled_keyword_id"],
                priority_score=metadata["priority_score"],
                selection_bucket=metadata["selection_bucket"],
            )

            loop_summary = run_selleramp_export_loop(
                keyword,
                is_first_keyword=is_first,
                sheet_updater=sheet_updater,
                keyword_store=keyword_store,
                return_summary=True,
                driver=managed_driver.driver,
                scraper_config=scraper_config,
            )
            is_first = False

            winners = int(loop_summary.get("winners_found", 0))
            maxed_out = bool(loop_summary.get("maxed_out", False))
            products_seen = int(loop_summary.get("products_seen", 0))
            sheet_rows_updated = int(loop_summary.get("sheet_rows_updated", 0))
            duplicate_asins_skipped = int(loop_summary.get("duplicate_asins_skipped", 0))
            error_message = loop_summary.get("error_message")

            keyword_status = "completed" if winners else "no_results"
            if error_message:
                keyword_status = "error"

            keyword_store.finish_run(
                keyword_run_id,
                status=keyword_status,
                products_exported=winners,
                sheet_rows_updated=sheet_rows_updated,
                products_seen=products_seen,
                duplicate_asins_skipped=duplicate_asins_skipped,
                error_message=error_message,
            )
            log_event(
                "scraper_keyword_run_completed",
                stage="Scrape keywords",
                keyword=keyword,
                keyword_run_id=keyword_run_id,
                status=keyword_status,
                winners_found=winners,
                products_seen=products_seen,
                sheet_rows_updated=sheet_rows_updated,
                duplicate_asins_skipped=duplicate_asins_skipped,
                maxed_out=maxed_out,
                error_message=error_message,
            )

            keyword_result = {
                "keyword": keyword,
                "status": keyword_status,
                "winners_found": winners,
                "products_seen": products_seen,
                "sheet_rows_updated": sheet_rows_updated,
                "duplicate_asins_skipped": duplicate_asins_skipped,
                "maxed_out": maxed_out,
                "error_message": error_message,
            }
            summary["keyword_results"].append(keyword_result)
            summary["keywords_completed"] += 1
            summary["products_seen"] += products_seen
            summary["sheet_rows_updated"] += sheet_rows_updated
            summary["duplicate_asins_skipped"] += duplicate_asins_skipped

            if error_message:
                summary["errors"].append({"keyword": keyword, "error": error_message})
                summary["stopped_reason"] = "error"
                print("\n[!] Stopping after scraper error to avoid untagged exports.")
                log_event(
                    "scraper_stage_stopping_after_error",
                    stage="Scrape keywords",
                    level="ERROR",
                    keyword=keyword,
                    error=error_message,
                )
                break

            if winners:
                total_winners_found += winners
                summary["total_winners_found"] = total_winners_found

            if maxed_out:
                maxed_out_keywords_count += 1
                summary["maxed_out_keywords"] = maxed_out_keywords_count
                if maxed_out_keywords_count >= 2:
                    print("\n==========================================")
                    print("[!] ABORT: Two keywords maxed out their retries. Halting the entire program to save lookups.")
                    print("==========================================")
                    summary["stopped_reason"] = "maxed_out_retries"
                    log_event(
                        "scraper_stage_stopping_after_maxed_out_retries",
                        stage="Scrape keywords",
                        level="WARNING",
                        maxed_out_keywords=maxed_out_keywords_count,
                    )
                    break

            print(f"\n>>> PROGRESS UPDATE: Global winners found so far: {total_winners_found}/{global_winner_limit}")
            time.sleep(scraper_config.timing.keyword_pause_seconds)
    finally:
        log_event(
            "scraper_driver_stopping",
            stage="Scrape keywords",
            elapsed_seconds=elapsed_seconds(stage_started_at),
        )
        managed_driver.quit()

    if summary["stopped_reason"] is None:
        summary["stopped_reason"] = "completed"

    summary["total_winners_found"] = total_winners_found
    print("\nScript entirely finished.")
    print_scrape_summary(summary)
    log_event(
        "scraper_stage_completed",
        stage="Scrape keywords",
        elapsed_seconds=elapsed_seconds(stage_started_at),
        **scrape_summary_for_log(summary),
    )
    return summary


def print_scrape_summary(summary):
    print("\nScrape summary:")
    print(f"  Keywords requested: {summary['keywords_requested']}")
    print(f"  Keywords attempted: {summary['keywords_attempted']}")
    print(f"  Keywords skipped: {summary['keywords_skipped']}")
    print(f"  Products seen: {summary['products_seen']}")
    print(f"  Winners exported: {summary['total_winners_found']}")
    print(f"  Sheet rows updated: {summary['sheet_rows_updated']}")
    print(f"  Duplicate ASINs skipped: {summary['duplicate_asins_skipped']}")
    print(f"  Stopped reason: {summary['stopped_reason']}")


def log_scraper_product_skip(keyword, page, product_index, reason, **fields):
    log_event(
        "scraper_product_skipped",
        stage="Scrape keywords",
        level="DEBUG",
        keyword=keyword,
        page=page,
        product_index=product_index,
        reason=reason,
        **fields,
    )


def scrape_summary_for_log(summary):
    return {
        "scheduler_run_id": summary.get("scheduler_run_id"),
        "keywords_requested": summary.get("keywords_requested"),
        "keywords_attempted": summary.get("keywords_attempted"),
        "keywords_skipped": summary.get("keywords_skipped"),
        "keywords_completed": summary.get("keywords_completed"),
        "products_seen": summary.get("products_seen"),
        "total_winners_found": summary.get("total_winners_found"),
        "sheet_rows_updated": summary.get("sheet_rows_updated"),
        "duplicate_asins_skipped": summary.get("duplicate_asins_skipped"),
        "maxed_out_keywords": summary.get("maxed_out_keywords"),
        "stopped_reason": summary.get("stopped_reason"),
        "error_count": len(summary.get("errors") or []),
        "keyword_result_count": len(summary.get("keyword_results") or []),
    }


def main():
    configure_pipeline_logging(PROJECT_ROOT)
    load_dotenv(DOTENV_PATH)
    scraper_config = load_scraper_config()
    global_winner_limit = scraper_config.scrape.global_winner_limit
    log_event(
        "scraper_main_started",
        stage="Scrape keywords",
        browser_mode=scraper_config.browser.mode,
        global_winner_limit=global_winner_limit,
    )

    keyword_store = KeywordStore()
    try:
        sheet_updater = ScrapeKeywordSheetUpdater.from_env(PROJECT_ROOT)
    except RuntimeError as error:
        print(f"[!] Google Sheets setup failed: {error}")
        log_event(
            "scraper_sheet_setup_failed",
            stage="Scrape keywords",
            level="ERROR",
            error=str(error),
        )
        return 1

    try:
        keywords_to_search, scheduler_run_id = select_keywords_with_scheduler(
            keyword_store=keyword_store,
            global_winner_limit=global_winner_limit,
        )
    except Exception as error:
        print(f"[!] Keyword scheduler failed: {error}")
        traceback.print_exc()
        log_event(
            "scraper_keyword_scheduler_failed",
            stage="Scrape keywords",
            level="ERROR",
            error_type=type(error).__name__,
            error=str(error),
            traceback=traceback.format_exc(),
        )
        return 1

    summary = scrape_keywords(
        keywords_to_search,
        keyword_store=keyword_store,
        sheet_updater=sheet_updater,
        global_winner_limit=global_winner_limit,
        scheduler_run_id=scheduler_run_id,
        scraper_config=scraper_config,
    )

    return 1 if summary["errors"] else 0

if __name__ == "__main__":
    raise SystemExit(main())
