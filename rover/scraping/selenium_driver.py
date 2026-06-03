from dataclasses import dataclass

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

from rover.scraping.config import ScraperConfig


@dataclass
class ManagedDriver:
    driver: webdriver.Chrome
    mode: str
    owns_browser: bool

    def quit(self) -> None:
        try:
            self.driver.quit()
        except Exception:
            return


def create_driver(config: ScraperConfig) -> ManagedDriver:
    mode = config.browser.mode
    options = Options()

    if mode == "debug":
        options.add_experimental_option("debuggerAddress", config.browser.debug_address)
        driver = webdriver.Chrome(options=options)
        return ManagedDriver(driver=driver, mode=mode, owns_browser=False)

    add_owned_browser_options(options, config)
    driver = webdriver.Chrome(options=options)

    if config.browser.start_url:
        driver.get(config.browser.start_url)

    return ManagedDriver(driver=driver, mode=mode, owns_browser=True)


def add_owned_browser_options(options: Options, config: ScraperConfig) -> None:
    options.add_argument(f"--window-size={config.browser.window_size}")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--profile-directory=Default")

    if config.browser.user_data_dir:
        options.add_argument(f"--user-data-dir={config.browser.user_data_dir}")
