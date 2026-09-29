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