from .models import SiteConfiguration


def set_site_config(**kwargs):
    """
    Test helper: mutate the SiteConfiguration singleton (field_name=value
    kwargs, e.g. ocr_backend='tesseract'). No explicit restore needed -
    each test runs inside its own DB transaction (TestCase) that's rolled
    back at teardown, same as any other model write in a test.
    """
    config = SiteConfiguration.load()
    for field, value in kwargs.items():
        setattr(config, field, value)
    config.save()
    return config
