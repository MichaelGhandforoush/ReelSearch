import re

# The uploader and caption as laid out by Video.to_document.
UPLOADER_PATTERN = re.compile(r"Uploader:\n {10}(.*?)\n\n {10}Caption:", re.S)
CAPTION_PATTERN = re.compile(
    r"\n {10}Caption:\n {10}(.*?)\n\n {10}Description:\n", re.S
)


class Video:
    def __init__(self, url):
        self.url = url
        self.path = ""
        self.frames = []
        self.transcript = ""
        self.info = ""
        self.user = ""
        self.caption = ""
        self.description = ""
        self.comments = ""
        self.embedding = ""
        self.stats = {}

    @staticmethod
    def parse_document(document):
        """Read the uploader and caption back out of a stored document."""
        fields = {}
        for name, pattern in (
            ("uploader", UPLOADER_PATTERN),
            ("caption", CAPTION_PATTERN),
        ):
            match = pattern.search(document or "")
            if match and match.group(1).strip():
                fields[name] = match.group(1).strip()
        return fields

    def to_document(self):
        comments = (
            f"""
          Comments:
          {self.comments}
          """
            if self.comments else ""
        )
        return f"""
          Uploader:
          {self.user}

          Caption:
          {self.caption}

          Description:
          {self.description}

          Transcript:
          {self.transcript}
          """ + comments