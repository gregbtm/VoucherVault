"""OCR backend integration for document extraction with multiple provider support."""

import os
import logging
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Dict, Optional, Any, List
from django.core.files.base import File
from myapp.models import Document

logger = logging.getLogger(__name__)


class OCRBackend(ABC):
    """Abstract base class for OCR backends."""

    @abstractmethod
    def extract_text(self, file_path: str) -> Optional[str]:
        """Extract text from document. Returns text or None on failure."""
        pass

    @abstractmethod
    def extract_structured_data(self, file_path: str) -> Optional[Dict[str, Any]]:
        """Extract structured data from document. Returns dict with extracted fields."""
        pass


class TesseractOCRBackend(OCRBackend):
    """Local Tesseract OCR backend (requires tesseract-ocr system package)."""

    def __init__(self):
        try:
            import pytesseract
            self.pytesseract = pytesseract
        except ImportError:
            logger.warning("pytesseract not installed, install with: pip install pytesseract")
            self.pytesseract = None

    def extract_text(self, file_path: str) -> Optional[str]:
        """Extract text from image using Tesseract."""
        if not self.pytesseract:
            logger.error("pytesseract not available")
            return None

        try:
            from PIL import Image
            img = Image.open(file_path)
            text = self.pytesseract.image_to_string(img)
            return text if text.strip() else None
        except Exception as e:
            logger.error(f"Tesseract extraction failed: {e}")
            return None

    def extract_structured_data(self, file_path: str) -> Optional[Dict[str, Any]]:
        """Extract structured data by parsing OCR text output."""
        text = self.extract_text(file_path)
        if not text:
            return None

        # Simple heuristic extraction from raw OCR text
        extracted = {'raw_text': text}

        # Try to find common voucher/gift card patterns
        lines = text.split('\n')

        # Look for currency amounts (£, $, €)
        for line in lines:
            if any(c in line for c in ['£', '$', '€', 'GBP', 'USD', 'EUR']):
                # Extract numbers from currency lines
                import re
                amounts = re.findall(r'[\d.]+', line)
                if amounts:
                    try:
                        extracted['value'] = Decimal(amounts[0])
                        extracted['value_confidence'] = Decimal('0.6')
                    except:
                        pass

        # Look for dates
        import re
        date_patterns = [
            r'\d{2}/\d{2}/\d{4}',  # DD/MM/YYYY
            r'\d{4}-\d{2}-\d{2}',  # YYYY-MM-DD
            r'\d{2}-\d{2}-\d{4}',  # MM-DD-YYYY
        ]

        for pattern in date_patterns:
            dates = re.findall(pattern, text)
            if dates:
                extracted['expiry_date'] = dates[0]
                extracted['expiry_date_confidence'] = Decimal('0.5')
                break

        return extracted if len(extracted) > 1 else None


class OpenAIVisionOCRBackend(OCRBackend):
    """Cloud-based OpenAI Vision API backend for more accurate extraction."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get('OPENAI_API_KEY')
        if not self.api_key:
            logger.warning("OPENAI_API_KEY not set, OpenAI Vision backend unavailable")

        try:
            from openai import OpenAI
            self.client = OpenAI(api_key=self.api_key) if self.api_key else None
        except ImportError:
            logger.warning("openai library not installed, install with: pip install openai")
            self.client = None

    def extract_text(self, file_path: str) -> Optional[str]:
        """Extract text from image using OpenAI Vision API."""
        if not self.client:
            logger.error("OpenAI client not available")
            return None

        try:
            import base64

            # Read and encode image
            with open(file_path, 'rb') as f:
                image_data = base64.standard_b64encode(f.read()).decode('utf-8')

            # Determine image format
            if file_path.lower().endswith('.pdf'):
                media_type = 'application/pdf'
            elif file_path.lower().endswith('.png'):
                media_type = 'image/png'
            elif file_path.lower().endswith(('.jpg', '.jpeg')):
                media_type = 'image/jpeg'
            else:
                media_type = 'image/jpeg'

            # Call Vision API
            message = self.client.messages.create(
                model='gpt-4-vision',
                max_tokens=1024,
                messages=[
                    {
                        'role': 'user',
                        'content': [
                            {
                                'type': 'image',
                                'source': {
                                    'type': 'base64',
                                    'media_type': media_type,
                                    'data': image_data,
                                },
                            },
                            {
                                'type': 'text',
                                'text': 'Extract all visible text from this voucher/gift card image. Be precise and include all text you can see.'
                            }
                        ],
                    }
                ],
            )

            return message.content[0].text if message.content else None
        except Exception as e:
            logger.error(f"OpenAI Vision extraction failed: {e}")
            return None

    def extract_structured_data(self, file_path: str) -> Optional[Dict[str, Any]]:
        """Extract structured data using OpenAI Vision with intelligent parsing."""
        if not self.client:
            logger.error("OpenAI client not available")
            return None

        try:
            import base64

            # Read and encode image
            with open(file_path, 'rb') as f:
                image_data = base64.standard_b64encode(f.read()).decode('utf-8')

            # Determine image format
            if file_path.lower().endswith('.pdf'):
                media_type = 'application/pdf'
            elif file_path.lower().endswith('.png'):
                media_type = 'image/png'
            else:
                media_type = 'image/jpeg'

            # Call Vision API with structured extraction prompt
            message = self.client.messages.create(
                model='gpt-4-vision',
                max_tokens=1024,
                messages=[
                    {
                        'role': 'user',
                        'content': [
                            {
                                'type': 'image',
                                'source': {
                                    'type': 'base64',
                                    'media_type': media_type,
                                    'data': image_data,
                                },
                            },
                            {
                                'type': 'text',
                                'text': '''Extract structured data from this voucher/gift card. Return JSON with these fields (only include if found):
{
  "issuer": "merchant name",
  "value": "monetary amount",
  "value_type": "currency code (GBP/USD/EUR)",
  "expiry_date": "expiration date in YYYY-MM-DD format",
  "redeem_code": "coupon/voucher code",
  "description": "brief description of what it is",
  "confidence_issuer": 0.0-1.0,
  "confidence_value": 0.0-1.0,
  "confidence_expiry": 0.0-1.0,
  "confidence_code": 0.0-1.0
}
Only return valid JSON, no other text.'''
                            }
                        ],
                    }
                ],
            )

            if not message.content:
                return None

            # Parse JSON response
            import json
            try:
                response_text = message.content[0].text
                # Try to extract JSON from response (might be wrapped in ```json```)
                if '```json' in response_text:
                    response_text = response_text.split('```json')[1].split('```')[0]
                elif '```' in response_text:
                    response_text = response_text.split('```')[1].split('```')[0]

                data = json.loads(response_text.strip())

                # Convert confidence values to Decimal
                for key in list(data.keys()):
                    if key.startswith('confidence_'):
                        data[key] = Decimal(str(data[key]))

                return data if data else None
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse Vision API JSON response: {e}")
                return None

        except Exception as e:
            logger.error(f"OpenAI Vision structured extraction failed: {e}")
            return None


class OCRExtractor:
    """Main OCR extraction service that selects and uses appropriate backend."""

    # Default backend priority order
    BACKEND_PRIORITY = ['openai_vision', 'tesseract']

    def __init__(self, preferred_backend: Optional[str] = None, api_key: Optional[str] = None):
        self.preferred_backend = preferred_backend
        self.api_key = api_key
        self._backends: Dict[str, OCRBackend] = {}
        self._init_backends()

    def _init_backends(self):
        """Initialize available OCR backends."""
        self._backends['tesseract'] = TesseractOCRBackend()
        self._backends['openai_vision'] = OpenAIVisionOCRBackend(api_key=self.api_key)

    def _get_backend(self) -> Optional[OCRBackend]:
        """Get the best available backend based on preference and availability."""
        # Try preferred backend first
        if self.preferred_backend and self.preferred_backend in self._backends:
            backend = self._backends[self.preferred_backend]
            if backend.client or self.preferred_backend == 'tesseract':
                return backend

        # Fall back to priority order
        for backend_name in self.BACKEND_PRIORITY:
            backend = self._backends.get(backend_name)
            if backend and (backend.client or backend_name == 'tesseract'):
                return backend

        logger.error("No OCR backend available")
        return None

    def extract_from_document(self, document: Document) -> Optional[Dict[str, Any]]:
        """Extract data from a Document object."""
        if not document.file:
            return None

        try:
            backend = self._get_backend()
            if not backend:
                return None

            # Get file path
            file_path = document.file.path if hasattr(document.file, 'path') else str(document.file)

            # Try structured extraction first (more reliable)
            result = backend.extract_structured_data(file_path)
            if result:
                return result

            # Fall back to text extraction
            text = backend.extract_text(file_path)
            return {'raw_text': text} if text else None

        except Exception as e:
            logger.error(f"Document extraction failed for {document.id}: {e}")
            return None

    def extract_from_file_path(self, file_path: str) -> Optional[Dict[str, Any]]:
        """Extract data from a file path."""
        try:
            backend = self._get_backend()
            if not backend:
                return None

            # Try structured extraction first
            result = backend.extract_structured_data(file_path)
            if result:
                return result

            # Fall back to text extraction
            text = backend.extract_text(file_path)
            return {'raw_text': text} if text else None

        except Exception as e:
            logger.error(f"File extraction failed for {file_path}: {e}")
            return None
