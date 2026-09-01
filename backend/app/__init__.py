import logging


# httpx logs full request URLs at INFO, including Microsoft Graph delta tokens.
logging.getLogger("httpx").setLevel(logging.WARNING)
