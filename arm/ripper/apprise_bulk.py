"""File to hold all functions pertaining to apprise"""
import logging
import apprise


def apprise_notify(apprise_cfg, title, body):
    """
    APPRISE NOTIFICATIONS\n
    Loads the apprise config file using native AppriseConfig, supporting the
    standard Apprise YAML format (urls: [...]) as well as legacy formats.
    :param apprise_cfg: The full path to the apprise.yaml file
    :param title: the message title
    :param body: the main body of the message
    :return: None
    """
    try:
        apobj = apprise.Apprise()
        config = apprise.AppriseConfig()
        config.add(apprise_cfg)
        apobj.add(config)
        result = apobj.notify(body, title=title)
        if result:
            logging.debug("Sent apprise notification successfully")
        else:
            logging.warning("Apprise notify returned False — check your apprise.yaml config and URLs")
    except Exception as error:
        logging.error(f"Failed sending apprise notification. {error}")
