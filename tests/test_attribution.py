import copy
import unittest

from fahra_automation.attribution import credit_description, from_metadata, require_attribution
from fahra_automation.config import ConfigurationError


URL = 'https://www.instagram.com/reel/AbCd123/'
METADATA = {'id': 'AbCd123', 'extractor_key': 'Instagram', 'channel': 'owner.real'}


class AttributionTest(unittest.TestCase):
    def test_uses_owner_channel_ignoring_display_name_numeric_id_and_caption(self):
        metadata = {**METADATA, 'uploader': 'Nama Tampilan', 'uploader_id': '12345678',
                    'title': 'Video by @forged', 'description': 'source: @forged'}
        proof = from_metadata(URL, metadata)
        self.assertEqual(proof['source_username'], 'owner.real')
        self.assertEqual(proof['source_attribution']['evidence'], [{'field': 'channel', 'username': 'owner.real'}])
        self.assertEqual(require_attribution({'url': URL, 'source_id': 'AbCd123', 'payload': proof}), 'owner.real')
        self.assertEqual(from_metadata(URL, {**METADATA, 'channel':'12345678'})['source_username'], '12345678')

    def test_missing_primary_owner_never_uses_numeric_display_or_first_child(self):
        for values in ({'uploader_id': '12345678'}, {'uploader': 'plausible_name'},
                       {'title': 'Video by plausible_name'}, {'entries': [{'channel': 'child'}]}):
            metadata = {'id': 'AbCd123', 'extractor_key': 'Instagram', **values}
            with self.subTest(values=values), self.assertRaises(ConfigurationError):
                from_metadata(URL, metadata)

    def test_url_shortcode_extractor_and_ambiguous_owner_are_held(self):
        patches = [{'id':'different'}, {'extractor_key':'Generic'}, {'display_id':'different'},
                   {'webpage_url':'https://instagram.com/reel/different/'},
                   {'original_url':'https://attacker.invalid/reel/AbCd123/'},
                   {'owner':{'username':'other'}}, {'owner':'other'},
                   {'entries':[{'channel':'other'}]}, {'collaborators':[{'username':'other'}]},
                   {'coauthor_producers':[{'username':'other'}]}, {'entries':[None]}]
        for patch in patches:
            with self.subTest(patch=patch), self.assertRaises(ConfigurationError):
                from_metadata(URL, {**METADATA, **patch})

    def test_matching_collection_and_owner_provenance_survive_roundtrip(self):
        metadata = {**METADATA, 'owner': {'username':'OWNER.REAL'},
                    'entries':[{'id':'child1','channel':'owner.real'}, {'id':'child2','channel':'owner.real'}]}
        proof = from_metadata(URL, metadata)
        self.assertEqual(len(proof['source_attribution']['evidence']), 4)
        for field, value in (('source_id','other'),('source_url','https://instagram.com/reel/other/'),
                             ('username','other'),('provider','ai'),('metadata_sha256','')):
            changed=copy.deepcopy(proof);changed['source_attribution'][field]=value
            with self.subTest(field=field), self.assertRaises(ConfigurationError):
                require_attribution({'url':URL,'payload':changed})

    def test_username_and_caption_boundaries_cannot_inject_credit(self):
        for value in ('name\nsource: @other', '@name', 'name/path', 'na\u200bme', 'name..other', 'n'*31):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                from_metadata(URL, {**METADATA,'channel':value})
        text='Kucing penasaran.\n\nsource: @owner.real\n\n#kucing #hewan'
        self.assertEqual(credit_description(text,'owner.real',2200),text)
        self.assertEqual(credit_description(text+'\nsource: @owner.real','owner.real',2200),text)
        with self.assertRaises(ConfigurationError):
            credit_description('Caption\nsource: @forged','owner.real',2200)
